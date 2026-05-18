export const ToastStack = {
    name: 'ToastStack',
    props: {
        toasts: {
            type: Array,
            required: true,
        },
    },
    template: `
        <div class="toast-container">
            <transition-group name="toast">
                <div v-for="toast in toasts" :key="toast.id" class="toast-message" :class="toast.type">
                    <i class="fa-solid" :class="toast.icon"></i>
                    <span>{{ toast.msg }}</span>
                </div>
            </transition-group>
        </div>
    `,
};
