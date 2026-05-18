export const FeedbackDialogs = {
    name: 'FeedbackDialogs',
    props: {
        selectState: { type: Object, required: true },
        confirmState: { type: Object, required: true },
        numberDialogState: { type: Object, required: true },
        handleSelect: { type: Function, required: true },
        closeSelectDialog: { type: Function, required: true },
        handleConfirm: { type: Function, required: true },
        handleNumberDialog: { type: Function, required: true },
        closeNumberDialog: { type: Function, required: true },
    },
    template: `
        <div>
            <transition name="fade">
                <div v-if="selectState.visible" class="confirm-mask">
                    <div class="confirm-box confirm-box-sm">
                        <div class="confirm-header">
                            <div class="confirm-title">{{ selectState.title }}</div>
                        </div>
                        <div class="confirm-desc">{{ selectState.msg }}</div>
                        <div class="confirm-select-actions">
                            <button v-for="(opt, idx) in selectState.options" :key="idx"
                                class="btn btn-ghost confirm-option-btn"
                                @click="handleSelect(opt.value)">
                                {{ opt.label }}
                            </button>
                        </div>
                        <div class="confirm-actions">
                            <button class="btn btn-secondary" @click="closeSelectDialog">取消</button>
                        </div>
                    </div>
                </div>
            </transition>

            <transition name="fade">
                <div v-if="confirmState.visible" class="confirm-mask">
                    <div class="confirm-box">
                        <div class="confirm-header">
                            <div class="confirm-icon-box" :class="confirmState.type">
                                <i class="fa-solid" :class="confirmState.icon"></i>
                            </div>
                            <div class="confirm-title">{{ confirmState.title }}</div>
                        </div>
                        <div class="confirm-desc">{{ confirmState.msg }}</div>
                        <div class="confirm-actions">
                            <button class="btn btn-secondary" @click="handleConfirm(false)">取消</button>
                            <button class="btn" :class="confirmState.confirmBtnClass" @click="handleConfirm(true)">{{ confirmState.confirmText }}</button>
                        </div>
                    </div>
                </div>
            </transition>

            <transition name="fade">
                <div v-if="numberDialogState.visible" class="confirm-mask">
                    <div class="confirm-box">
                        <div class="confirm-header">
                            <div class="confirm-icon-box info">
                                <i class="fa-solid fa-percent"></i>
                            </div>
                            <div class="confirm-title">{{ numberDialogState.title }}</div>
                        </div>
                        <div class="confirm-desc confirm-desc-plain">{{ numberDialogState.msg }}</div>
                        <div class="confirm-input-row">
                            <input
                                v-model="numberDialogState.value"
                                type="number"
                                min="0"
                                max="99.99"
                                step="0.01"
                                class="form-control confirm-input"
                                :placeholder="numberDialogState.placeholder"
                                @keyup.enter="handleNumberDialog(true)"
                            >
                            <span class="confirm-input-suffix">{{ numberDialogState.suffix }}</span>
                        </div>
                        <div class="confirm-actions">
                            <button class="btn btn-secondary" @click="closeNumberDialog">取消</button>
                            <button class="btn btn-primary" @click="handleNumberDialog(true)">确定</button>
                        </div>
                    </div>
                </div>
            </transition>
        </div>
    `,
};
