import axios from 'axios';
import { createApp, reactive, ref, onMounted } from 'vue';
import './style.css';

createApp({
    setup() {
        const form = reactive({ username: '', password: '' });
        const errorMsg = ref('');
        const loading = ref(false);
        const isError = ref(false);

        const wallRows = reactive([[], [], [], [], []]);
        const hasCovers = ref(false);

        const startParticles = () => {
            const canvas = document.getElementById('dynamic-bg');
            if (!canvas) {
                requestAnimationFrame(startParticles);
                return;
            }

            const ctx = canvas.getContext('2d');
            let width;
            let height;
            let particles = [];
            const particleCount = 80;

            function resize() {
                width = canvas.width = window.innerWidth;
                height = canvas.height = window.innerHeight;
            }

            class Particle {
                constructor() {
                    this.reset();
                    this.y = Math.random() * height;
                }

                reset() {
                    this.x = Math.random() * width;
                    this.y = height + Math.random() * 100;
                    this.size = Math.random() * 2 + 0.5;
                    this.speedY = Math.random() * 0.5 + 0.2;
                    this.opacity = Math.random() * 0.5 + 0.1;
                }

                update() {
                    this.y -= this.speedY;
                    if (this.y < -10) this.reset();
                }

                draw() {
                    ctx.beginPath();
                    ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
                    ctx.fillStyle = `rgba(255, 255, 255, ${this.opacity})`;
                    ctx.fill();
                }
            }

            function initParticles() {
                particles = [];
                for (let i = 0; i < particleCount; i++) particles.push(new Particle());
            }

            function animate() {
                ctx.clearRect(0, 0, width, height);
                particles.forEach((p) => {
                    p.update();
                    p.draw();
                });
                requestAnimationFrame(animate);
            }

            window.addEventListener('resize', () => {
                resize();
                initParticles();
            });
            resize();
            initParticles();
            animate();
        };

        const distributeCovers = (covers) => {
            const minItems = 12;
            const rowCount = 5;
            const rows = Array.from({ length: rowCount }, () => []);
            covers.forEach((item, idx) => {
                rows[idx % rowCount].push(item);
            });
            for (let i = 0; i < rowCount; i++) {
                let current = [...rows[i]];
                if (current.length === 0) current = [...covers];
                if (current.length > 0) {
                    while (current.length < minItems) current = [...current, ...current];
                    wallRows[i] = [...current, ...current];
                }
            }
        };

        const initBackground = async () => {
            try {
                const configRes = await axios.get('/api/config_302/get');
                const embys = Array.isArray(configRes.data?.embys) ? configRes.data.embys : [];
                if (embys.length === 0) return;

                const svr = embys[0];
                if (svr.url && svr.key) {
                    const res = await axios.post('/api/library_covers', {
                        url: svr.url,
                        key: svr.key,
                        public_host: svr.public_host,
                    });

                    const covers = res.data.libraries || [];
                    if (covers.length > 0) {
                        distributeCovers(covers);
                        hasCovers.value = true;
                    }
                }
            } catch (e) {
                console.log('Bg load fail');
            }
        };

        const triggerShake = () => {
            isError.value = true;
            setTimeout(() => {
                isError.value = false;
            }, 400);
        };

        const login = async () => {
            if (!form.username || !form.password) {
                errorMsg.value = '请输入用户名和密码';
                triggerShake();
                return;
            }
            loading.value = true;
            errorMsg.value = '';
            try {
                const res = await axios.post('/api/login', form);
                if (res.data.status === 'ok') {
                    localStorage.setItem('isLoggedIn', 'true');
                    window.location.href = 'index.html';
                }
            } catch (e) {
                errorMsg.value = '登录失败：账号或密码错误';
                triggerShake();
            } finally {
                loading.value = false;
            }
        };

        onMounted(() => {
            startParticles();
            initBackground();
        });

        return { form, login, errorMsg, loading, isError, wallRows, hasCovers };
    },
}).mount('#login-app');
