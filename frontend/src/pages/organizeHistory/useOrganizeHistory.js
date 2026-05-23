import axios from 'axios';
import { reactive } from 'vue';

export function useOrganizeHistory({ showToast }) {
    const organizeHistory = reactive({
        loading: false,
        categories: [],
        records: [],
        activeCategory: 'organize_success',
        keyword: '',
        pendingKeyword: '',
        total: 0,
        updatedAt: '',
        limit: 200,
        page: 1,
        pageSize: 50,
        pageCount: 1,
        error: '',
    });

    const fetchOrganizeHistory = async () => {
        organizeHistory.loading = true;
        organizeHistory.error = '';
        try {
            const res = await axios.get('/api/organize-history/records', {
                params: {
                    category: organizeHistory.activeCategory,
                    keyword: organizeHistory.keyword,
                    page: organizeHistory.page,
                    page_size: organizeHistory.pageSize,
                },
            });
            organizeHistory.categories = Array.isArray(res.data?.categories) ? res.data.categories : [];
            organizeHistory.records = Array.isArray(res.data?.records) ? res.data.records : [];
            organizeHistory.total = Number(res.data?.total || 0);
            organizeHistory.page = Number(res.data?.page || organizeHistory.page || 1);
            organizeHistory.pageSize = Number(res.data?.page_size || organizeHistory.pageSize || 50);
            organizeHistory.pageCount = Number(res.data?.page_count || 1);
            organizeHistory.updatedAt = res.data?.updated_at || '';
        } catch (e) {
            organizeHistory.error = e.response?.data?.detail || e.message || '读取整理记录失败';
            if (showToast) showToast(organizeHistory.error, 'error');
        } finally {
            organizeHistory.loading = false;
        }
    };

    const selectOrganizeHistoryCategory = (category) => {
        organizeHistory.activeCategory = category || 'organize_success';
        organizeHistory.page = 1;
        fetchOrganizeHistory();
    };

    const applyOrganizeHistorySearch = () => {
        organizeHistory.keyword = (organizeHistory.pendingKeyword || '').trim();
        organizeHistory.page = 1;
        fetchOrganizeHistory();
    };

    const clearOrganizeHistorySearch = () => {
        organizeHistory.pendingKeyword = '';
        organizeHistory.keyword = '';
        organizeHistory.page = 1;
        fetchOrganizeHistory();
    };

    const changeOrganizeHistoryPage = (page) => {
        const nextPage = Math.max(1, Math.min(Number(page || 1), organizeHistory.pageCount || 1));
        if (nextPage === organizeHistory.page) return;
        organizeHistory.page = nextPage;
        fetchOrganizeHistory();
    };

    return {
        organizeHistory,
        fetchOrganizeHistory,
        selectOrganizeHistoryCategory,
        applyOrganizeHistorySearch,
        clearOrganizeHistorySearch,
        changeOrganizeHistoryPage,
    };
}
