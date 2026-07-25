(() => {
    const shell = document.querySelector('[data-app-shell]');
    const menuButton = document.querySelector('[data-menu-toggle]');
    const overlay = document.querySelector('[data-menu-overlay]');
    const sidebar = document.querySelector('[data-app-sidebar]');
    const toastRegion = document.getElementById('uiToastRegion');
    const confirmDialog = document.getElementById('uiConfirmDialog');
    const confirmTitle = document.getElementById('uiConfirmTitle');
    const confirmMessage = document.getElementById('uiConfirmMessage');
    const confirmAccept = document.getElementById('uiConfirmAccept');
    const confirmCancel = document.getElementById('uiConfirmCancel');
    let lastMenuFocus = null;
    let pendingConfirmation = null;

    function setMenu(open) {
        if (!shell || !menuButton) return;
        shell.classList.toggle('is-menu-open', open);
        menuButton.setAttribute('aria-expanded', String(open));
        document.body.style.overflow = open ? 'hidden' : '';
        if (open) {
            lastMenuFocus = document.activeElement;
            sidebar?.querySelector('a')?.focus();
        } else if (lastMenuFocus instanceof HTMLElement) {
            lastMenuFocus.focus();
        }
    }

    menuButton?.addEventListener('click', () => {
        setMenu(!shell.classList.contains('is-menu-open'));
    });
    overlay?.addEventListener('click', () => setMenu(false));
    sidebar?.querySelectorAll('a').forEach(link => {
        link.addEventListener('click', () => setMenu(false));
    });

    document.addEventListener('keydown', event => {
        if (event.key === 'Escape' && shell?.classList.contains('is-menu-open')) {
            setMenu(false);
        }
    });

    function toast(message, kind = 'neutral', timeout = 4200) {
        if (!toastRegion) return;
        const item = document.createElement('div');
        item.className = 'ui-toast';
        item.dataset.kind = kind;
        item.setAttribute('role', kind === 'error' ? 'alert' : 'status');

        const text = document.createElement('div');
        text.className = 'ui-toast__message';
        text.textContent = String(message);
        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'ui-toast__close';
        close.setAttribute('aria-label', '关闭通知');
        close.textContent = '×';
        close.addEventListener('click', () => item.remove());

        item.append(text, close);
        toastRegion.append(item);
        if (timeout > 0) window.setTimeout(() => item.remove(), timeout);
    }

    function confirmAction(options = {}) {
        if (!confirmDialog) return Promise.resolve(false);
        if (pendingConfirmation) settleConfirmation(false);

        confirmTitle.textContent = options.title || '确认操作';
        confirmMessage.textContent = options.message || '确定继续吗？';
        confirmAccept.textContent = options.confirmLabel || '确认';
        confirmAccept.classList.toggle('btn-danger', options.kind === 'danger');
        confirmDialog.showModal();

        return new Promise(resolve => {
            pendingConfirmation = resolve;
        });
    }

    function settleConfirmation(result) {
        if (!pendingConfirmation) return;
        const resolve = pendingConfirmation;
        pendingConfirmation = null;
        if (confirmDialog.open) confirmDialog.close();
        resolve(result);
    }

    confirmAccept?.addEventListener('click', () => settleConfirmation(true));
    confirmCancel?.addEventListener('click', () => settleConfirmation(false));
    confirmDialog?.addEventListener('cancel', event => {
        event.preventDefault();
        settleConfirmation(false);
    });
    confirmDialog?.addEventListener('close', () => {
        if (pendingConfirmation) settleConfirmation(false);
    });

    window.PixivUI = Object.freeze({
        toast,
        confirm: confirmAction,
        openMenu: () => setMenu(true),
        closeMenu: () => setMenu(false),
    });
})();
