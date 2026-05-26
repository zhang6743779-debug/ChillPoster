export const ToastStack = {
    name: 'ToastStack',
    props: {
        toasts: {
            type: Array,
            required: true,
        },
    },
    template: `
        <div class="toast-container" role="status" aria-live="polite">
            <transition-group name="toast">
                <div v-for="toast in toasts" :key="toast.id" class="toast-message" :class="toast.type">
                    <span class="toast-icon-wrap">
                        <i class="fa-solid" :class="toast.icon"></i>
                    </span>
                    <span class="toast-text">{{ toast.msg }}</span>
                </div>
            </transition-group>
        </div>
    `,
};
