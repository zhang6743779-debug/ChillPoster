import { reactive, ref } from 'vue';

export function useFeedbackDialogs() {
    const toasts = ref([]);
    let toastId = 0;

    const showToast = (msg, type = 'success') => {
        const id = toastId++;
        let icon = 'fa-circle-check';
        if (type === 'error') icon = 'fa-circle-xmark';
        if (type === 'info') icon = 'fa-circle-info';
        
        toasts.value.push({ id, msg, type, icon });
        
        setTimeout(() => {
            const idx = toasts.value.findIndex(t => t.id === id);
            if (idx !== -1) toasts.value.splice(idx, 1);
        }, 3000);
    };

    // ==========================================
    // 2. Confirm 弹窗系统
    // ==========================================
    const confirmState = reactive({
        visible: false, title: '', msg: '', type: 'warning', icon: 'fa-triangle-exclamation',
        confirmText: '确定', confirmBtnClass: 'btn-primary', resolve: null
    });

    // 选择对话框状态
    const selectState = reactive({
        visible: false, title: '', msg: '', options: [], resolve: null
    });

    const numberDialogState = reactive({
        visible: false,
        title: '',
        msg: '',
        placeholder: '',
        suffix: '%',
        value: '',
        validator: null,
        resolve: null,
    });

    const showConfirm = (title, msg, type = 'warning') => {
        return new Promise((resolve) => {
            confirmState.title = title;
            confirmState.msg = msg;
            confirmState.type = type;
            confirmState.visible = true;
            confirmState.resolve = resolve;

            if (type === 'danger') {
                confirmState.icon = 'fa-trash-can';
                confirmState.confirmText = '确认删除';
                confirmState.confirmBtnClass = 'btn-danger';
            } else if (type === 'warning') {
                confirmState.icon = 'fa-triangle-exclamation';
                confirmState.confirmText = '确定执行';
                confirmState.confirmBtnClass = 'btn-primary';
            } else {
                confirmState.icon = 'fa-circle-info';
                confirmState.confirmText = '确定';
                confirmState.confirmBtnClass = 'btn-primary';
            }
        });
    };

    const handleConfirm = (result) => {
        confirmState.visible = false;
        if (confirmState.resolve) { confirmState.resolve(result); confirmState.resolve = null; }
    };

    const showSelectDialog = (title, msg, options) => {
        return new Promise((resolve) => {
            selectState.title = title;
            selectState.msg = msg;
            selectState.options = options;
            selectState.visible = true;
            selectState.resolve = resolve;
        });
    };

    const handleSelect = (index) => {
        selectState.visible = false;
        if (selectState.resolve) { selectState.resolve(index); selectState.resolve = null; }
    };

    const closeSelectDialog = () => {
        selectState.visible = false;
        if (selectState.resolve) { selectState.resolve(null); selectState.resolve = null; }
    };

    const showNumberDialog = (title, msg, defaultValue = '', placeholder = '', validator = null) => {
        return new Promise((resolve) => {
            numberDialogState.title = title;
            numberDialogState.msg = msg;
            numberDialogState.value = defaultValue === null || defaultValue === undefined ? '' : String(defaultValue);
            numberDialogState.placeholder = placeholder;
            numberDialogState.validator = validator;
            numberDialogState.visible = true;
            numberDialogState.resolve = resolve;
        });
    };

    const handleNumberDialog = (result) => {
        const value = numberDialogState.value;
        if (result && typeof numberDialogState.validator === 'function') {
            const errorMessage = numberDialogState.validator(value);
            if (errorMessage) {
                showToast(errorMessage, 'error');
                return;
            }
        }
        numberDialogState.visible = false;
        numberDialogState.validator = null;
        if (numberDialogState.resolve) {
            numberDialogState.resolve(result ? value : null);
            numberDialogState.resolve = null;
        }
    };

    const closeNumberDialog = () => {
        handleNumberDialog(false);
    };

    return {
        toasts,
        showToast,
        confirmState,
        handleConfirm,
        showConfirm,
        selectState,
        handleSelect,
        closeSelectDialog,
        showSelectDialog,
        numberDialogState,
        handleNumberDialog,
        closeNumberDialog,
        showNumberDialog,
    };
}
