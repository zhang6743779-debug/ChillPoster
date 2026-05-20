import { computed, nextTick, ref } from 'vue';

export function useShellNavigation({ tab, allSearchItems, onResize }) {
    const sidebarHover = ref(false);
    const isImmersiveMode = ref(false);

    const toggleSidebar = () => {
        isImmersiveMode.value = !isImmersiveMode.value;
        sidebarHover.value = false;
    };

    const isMobileViewport = () => window.innerWidth < 769;
    const isMobile = ref(isMobileViewport());
    const isStandaloneWebApp = window.navigator.standalone === true || window.matchMedia('(display-mode: standalone)').matches;
    document.documentElement.classList.toggle('standalone-webapp', isStandaloneWebApp);

    const initialPanelId = tab.value && tab.value !== 'dashboard' ? tab.value : null;
    const openPanels = ref(initialPanelId ? [initialPanelId] : []);
    const focusedPanel = ref(initialPanelId);
    const showSettingsDrawer = ref(false);
    const showCoverDrawer = ref(false);
    const showStorageDrawer = ref(false);
    const showToolboxDrawer = ref(false);
    const showSpotlight = ref(false);
    const spotlightQuery = ref('');
    const spotlightFocusIndex = ref(0);
    const dockHoverIndex = ref(null);
    const spotlightInputRef = ref(null);
    const theme = ref('dark');
    const THEME_STORAGE_KEY = 'chillposter-theme';

    const applyTheme = (nextTheme) => {
        const normalizedTheme = !isMobileViewport() && nextTheme === 'light' ? 'light' : 'dark';
        theme.value = normalizedTheme;
        document.documentElement.dataset.theme = normalizedTheme;
        localStorage.setItem(THEME_STORAGE_KEY, normalizedTheme);
    };

    const resolveInitialTheme = () => {
        if (isMobileViewport()) return 'dark';
        const savedTheme = localStorage.getItem(THEME_STORAGE_KEY);
        if (savedTheme === 'light' || savedTheme === 'dark') {
            return savedTheme;
        }
        return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    };

    const toggleTheme = () => {
        if (isMobileViewport()) {
            applyTheme('dark');
            return;
        }
        applyTheme(theme.value === 'light' ? 'dark' : 'light');
    };

    const togglePanel = (panelId) => {
        if (focusedPanel.value === panelId) {
            closePanel(panelId);
        } else if (openPanels.value.includes(panelId)) {
            focusPanel(panelId);
        } else {
            openPanels.value = [panelId];
            focusedPanel.value = panelId;
            tab.value = panelId;
        }
        showSettingsDrawer.value = false;
    };

    const closePanel = (panelId) => {
        openPanels.value = openPanels.value.filter(id => id !== panelId);
        if (focusedPanel.value === panelId) {
            if (openPanels.value.length > 0) {
                focusedPanel.value = openPanels.value[openPanels.value.length - 1];
                tab.value = focusedPanel.value;
            } else {
                focusedPanel.value = null;
                tab.value = 'dashboard';
            }
        }
    };

    const focusPanel = (panelId) => {
        if (openPanels.value.includes(panelId)) {
            focusedPanel.value = panelId;
            tab.value = panelId;
        }
    };

    const goHome = () => {
        openPanels.value = [];
        focusedPanel.value = null;
        tab.value = 'dashboard';
        closeDockDrawers();
    };

    const buildDrawerStyle = (e, drawerWidth = 480) => {
        if (!e) return {};
        const btn = e.currentTarget;
        const rect = btn.getBoundingClientRect();
        const btnCenterX = rect.left + rect.width / 2;
        let left = btnCenterX - drawerWidth / 2;
        left = Math.max(8, Math.min(left, window.innerWidth - drawerWidth - 8));
        return {
            position: 'fixed',
            bottom: (window.innerHeight - rect.top + 12) + 'px',
            left: left + 'px',
            right: 'auto',
            width: drawerWidth + 'px',
            borderRadius: '16px',
        };
    };

    const closeDockDrawers = () => {
        showSettingsDrawer.value = false;
        showCoverDrawer.value = false;
        showStorageDrawer.value = false;
        showToolboxDrawer.value = false;
    };

    const settingsDrawerStyle = ref({});
    const coverDrawerStyle = ref({});
    const storageDrawerStyle = ref({});
    const toolboxDrawerStyle = ref({});
    const toggleSettingsDrawer = (e) => {
        if (isMobile.value) return;
        const nextState = !showSettingsDrawer.value;
        closeDockDrawers();
        showSettingsDrawer.value = nextState;
        if (showSettingsDrawer.value) {
            settingsDrawerStyle.value = buildDrawerStyle(e);
        }
    };

    const toggleCoverDrawer = (e) => {
        if (isMobile.value) return;
        const nextState = !showCoverDrawer.value;
        closeDockDrawers();
        showCoverDrawer.value = nextState;
        if (showCoverDrawer.value) {
            coverDrawerStyle.value = buildDrawerStyle(e);
        }
    };

    const toggleStorageDrawer = (e) => {
        if (isMobile.value) return;
        const nextState = !showStorageDrawer.value;
        closeDockDrawers();
        showStorageDrawer.value = nextState;
        if (showStorageDrawer.value) {
            storageDrawerStyle.value = buildDrawerStyle(e);
        }
    };

    const toggleToolboxDrawer = (e) => {
        if (isMobile.value) return;
        const nextState = !showToolboxDrawer.value;
        closeDockDrawers();
        showToolboxDrawer.value = nextState;
        if (showToolboxDrawer.value) {
            toolboxDrawerStyle.value = buildDrawerStyle(e);
        }
    };

    const openFromSettings = (id) => {
        closeDockDrawers();
        togglePanel(id);
    };

    const showSpotlightPanel = () => {
        if (isMobile.value) return;
        showSpotlight.value = true;
        spotlightQuery.value = '';
        spotlightFocusIndex.value = 0;
        nextTick(() => {
            if (spotlightInputRef.value) spotlightInputRef.value.focus();
        });
    };

    const spotlightResults = computed(() => {
        const q = spotlightQuery.value.toLowerCase().trim();
        if (!q) return allSearchItems;
        return allSearchItems.filter(item =>
            item.label.toLowerCase().includes(q) ||
            item.id.toLowerCase().includes(q) ||
            item.group.toLowerCase().includes(q)
        );
    });

    const jumpToItem = (id) => {
        showSpotlight.value = false;
        if (id === 'dashboard') {
            goHome();
        } else {
            if (!openPanels.value.includes(id)) {
                openPanels.value = [id];
            }
            focusedPanel.value = id;
            tab.value = id;
        }
    };

    const selectSpotlightResult = () => {
        if (spotlightResults.value.length > 0) {
            const item = spotlightResults.value[spotlightFocusIndex.value];
            if (item) jumpToItem(item.id);
        }
    };

    const spotlightUp = () => {
        if (spotlightFocusIndex.value > 0) {
            spotlightFocusIndex.value--;
        }
    };

    const spotlightDown = () => {
        if (spotlightFocusIndex.value < spotlightResults.value.length - 1) {
            spotlightFocusIndex.value++;
        }
    };

    const handleKeydown = (e) => {
        if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
            e.preventDefault();
            showSpotlightPanel();
        }
        if (e.key === 'Escape') {
            if (showSpotlight.value) {
                showSpotlight.value = false;
            } else if (showSettingsDrawer.value || showCoverDrawer.value || showStorageDrawer.value) {
                closeDockDrawers();
            }
        }
    };

    const closeDesktopOverlays = () => {
        closeDockDrawers();
        showSpotlight.value = false;
        dockHoverIndex.value = null;
        spotlightQuery.value = '';
        spotlightFocusIndex.value = 0;
    };

    const handleResize = () => {
        const nextIsMobile = isMobileViewport();
        if (nextIsMobile && !isMobile.value) {
            closeDesktopOverlays();
        }
        isMobile.value = nextIsMobile;
        if (nextIsMobile && theme.value !== 'dark') {
            applyTheme('dark');
        }
        if (typeof onResize === 'function') {
            onResize();
        }
    };

    applyTheme(resolveInitialTheme());

    return {
        sidebarHover,
        isImmersiveMode,
        toggleSidebar,
        isMobile,
        openPanels,
        focusedPanel,
        showSettingsDrawer,
        showCoverDrawer,
        showStorageDrawer,
        showToolboxDrawer,
        showSpotlight,
        spotlightQuery,
        spotlightFocusIndex,
        dockHoverIndex,
        spotlightInputRef,
        theme,
        settingsDrawerStyle,
        coverDrawerStyle,
        storageDrawerStyle,
        toolboxDrawerStyle,
        toggleTheme,
        togglePanel,
        closePanel,
        focusPanel,
        goHome,
        closeDockDrawers,
        closeDesktopOverlays,
        toggleSettingsDrawer,
        toggleCoverDrawer,
        toggleStorageDrawer,
        toggleToolboxDrawer,
        openFromSettings,
        showSpotlightPanel,
        spotlightResults,
        jumpToItem,
        selectSpotlightResult,
        spotlightUp,
        spotlightDown,
        handleKeydown,
        handleResize,
    };
}
