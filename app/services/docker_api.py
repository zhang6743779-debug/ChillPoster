import http.client
import json
import os
import re
import socket
import urllib.parse


DOCKER_SOCKET = "/var/run/docker.sock"


class DockerApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Docker API {status}: {message}")
        self.status = status
        self.message = message


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str = DOCKER_SOCKET, timeout: float = 30):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def _loads_docker_json(text: str):
    stripped = text.strip()
    if not stripped:
        return ""
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        items = []
        for line in stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                return text
        return items if items else text


class DockerAPI:
    def __init__(self, socket_path: str = DOCKER_SOCKET, timeout: float = 30):
        self.socket_path = socket_path
        self.timeout = timeout

    def request(self, method: str, path: str, body=None, headers: dict | None = None):
        payload = None
        req_headers = headers.copy() if headers else {}
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        conn = UnixHTTPConnection(self.socket_path, timeout=self.timeout)
        try:
            conn.request(method, path, body=payload, headers=req_headers)
            resp = conn.getresponse()
            raw = resp.read()
        finally:
            conn.close()

        text = raw.decode("utf-8", errors="replace") if raw else ""
        if resp.status >= 400:
            raise DockerApiError(resp.status, text.strip() or resp.reason)
        content_type = resp.getheader("Content-Type", "")
        if "application/json" in content_type and text.strip():
            return _loads_docker_json(text)
        if text.strip().startswith("{") or text.strip().startswith("["):
            return _loads_docker_json(text)
        return text

    def ping(self):
        return self.request("GET", "/_ping")

    def version(self):
        return self.request("GET", "/version")

    def inspect_container(self, container_id: str):
        quoted = urllib.parse.quote(str(container_id), safe="")
        return self.request("GET", f"/containers/{quoted}/json")

    def pull_image(self, image: str):
        repo, tag = split_image_ref(image)
        query = urllib.parse.urlencode({"fromImage": repo, "tag": tag})
        result = self.request("POST", f"/images/create?{query}")
        if isinstance(result, list):
            for item in result:
                if not isinstance(item, dict):
                    continue
                error = item.get("error") or item.get("errorDetail", {}).get("message")
                if error:
                    raise DockerApiError(500, str(error))
        return result

    def create_container(self, name: str, payload: dict):
        query = urllib.parse.urlencode({"name": name})
        return self.request("POST", f"/containers/create?{query}", payload)

    def start_container(self, container_id: str):
        quoted = urllib.parse.quote(str(container_id), safe="")
        return self.request("POST", f"/containers/{quoted}/start")

    def stop_container(self, container_id: str, timeout: int = 20):
        quoted = urllib.parse.quote(str(container_id), safe="")
        query = urllib.parse.urlencode({"t": int(timeout)})
        return self.request("POST", f"/containers/{quoted}/stop?{query}")

    def delete_container(self, container_id: str, force: bool = False):
        quoted = urllib.parse.quote(str(container_id), safe="")
        query = urllib.parse.urlencode({"force": "1" if force else "0"})
        return self.request("DELETE", f"/containers/{quoted}?{query}")


def split_image_ref(image: str) -> tuple[str, str]:
    value = str(image or "").strip()
    if not value:
        return "chillne/chillposter", "latest"
    last_slash = value.rfind("/")
    last_colon = value.rfind(":")
    if last_colon > last_slash:
        return value[:last_colon], value[last_colon + 1:] or "latest"
    return value, "latest"


def get_current_container_id(api: DockerAPI | None = None) -> str:
    candidates = []
    hostname = os.getenv("HOSTNAME", "").strip()
    if hostname:
        candidates.append(hostname)
    try:
        with open("/proc/self/cgroup", "r", encoding="utf-8") as f:
            text = f.read()
        candidates.extend(re.findall(r"[0-9a-f]{64}", text))
    except Exception:
        pass

    docker = api or DockerAPI()
    for candidate in candidates:
        try:
            info = docker.inspect_container(candidate)
            cid = str(info.get("Id") or "").strip()
            if cid:
                return cid
        except Exception:
            continue
    return ""


def _copy_existing(source: dict, keys: list[str]) -> dict:
    copied = {}
    for key in keys:
        value = source.get(key)
        if value is not None:
            copied[key] = value
    return copied


def _mounts_from_inspect(info: dict) -> list[dict]:
    mounts = []
    for item in info.get("Mounts") or []:
        mount_type = item.get("Type")
        destination = item.get("Destination")
        if mount_type not in {"bind", "volume"} or not destination:
            continue
        source = item.get("Source") if mount_type == "bind" else item.get("Name")
        if not source:
            continue
        mount = {
            "Type": mount_type,
            "Source": source,
            "Target": destination,
            "ReadOnly": not bool(item.get("RW", True)),
        }
        if mount_type == "volume" and item.get("Driver"):
            mount["VolumeOptions"] = {"DriverConfig": {"Name": item.get("Driver")}}
        mounts.append(mount)
    return mounts


def build_replacement_container_payload(info: dict, image: str) -> dict:
    config = info.get("Config") or {}
    host_config = info.get("HostConfig") or {}
    old_id = str(info.get("Id") or "")
    old_name = str(info.get("Name") or "").strip("/")

    payload = _copy_existing(config, [
        "Domainname", "User", "AttachStdin", "AttachStdout", "AttachStderr",
        "Tty", "OpenStdin", "StdinOnce", "Env", "Cmd", "Entrypoint", "WorkingDir",
        "ExposedPorts", "Labels", "StopSignal", "Healthcheck", "Shell",
    ])
    payload["Image"] = image

    new_host_config = _copy_existing(host_config, [
        "Binds", "PortBindings", "RestartPolicy", "AutoRemove", "VolumeDriver", "VolumesFrom",
        "CapAdd", "CapDrop", "Dns", "DnsOptions", "DnsSearch", "ExtraHosts", "GroupAdd",
        "IpcMode", "Cgroup", "Links", "OomScoreAdj", "PidMode", "Privileged", "PublishAllPorts",
        "ReadonlyRootfs", "SecurityOpt", "StorageOpt", "Tmpfs", "UTSMode", "UsernsMode",
        "ShmSize", "Sysctls", "Runtime", "ConsoleSize", "Isolation", "CpuShares", "Memory",
        "MemorySwap", "MemoryReservation", "NanoCpus", "CpusetCpus", "CpusetMems", "Devices",
        "DeviceCgroupRules", "DeviceRequests", "LogConfig", "NetworkMode",
    ])
    bind_targets = set()
    for bind in new_host_config.get("Binds") or []:
        parts = str(bind).split(":")
        if len(parts) >= 2:
            bind_targets.add(parts[1])
    mounts = [mount for mount in _mounts_from_inspect(info) if mount.get("Target") not in bind_targets]
    if mounts:
        payload["Mounts"] = mounts
    new_host_config["AutoRemove"] = False
    payload["HostConfig"] = new_host_config

    networks = ((info.get("NetworkSettings") or {}).get("Networks") or {})
    endpoint_config = {}
    for network_name, network in networks.items():
        aliases = []
        for alias in network.get("Aliases") or []:
            if alias and alias not in {old_id[:12], old_id, old_name}:
                aliases.append(alias)
        endpoint = {}
        if aliases:
            endpoint["Aliases"] = sorted(set(aliases))
        if network.get("IPAMConfig"):
            endpoint["IPAMConfig"] = network.get("IPAMConfig")
        endpoint_config[network_name] = endpoint
    if endpoint_config:
        payload["NetworkingConfig"] = {"EndpointsConfig": endpoint_config}

    return payload
