document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) {
        return;
    }
    const message = form.dataset.confirm;
    if (message && !window.confirm(message)) {
        event.preventDefault();
    }
});

document.addEventListener("click", async (event) => {
    const target = event.target;
    if (!(target instanceof Element)) {
        return;
    }
    const button = target.closest("[data-workspace-picker]");
    if (!(button instanceof HTMLButtonElement)) {
        return;
    }

    event.preventDefault();

    const form = button.closest("form");
    const endpoint = button.dataset.endpoint;
    const targetSelector = button.dataset.targetInput;
    const feedbackSelector = button.dataset.feedbackSelector;
    const feedback = feedbackSelector ? document.querySelector(feedbackSelector) : null;
    const input = targetSelector && form ? form.querySelector(targetSelector) : null;
    const csrfInput = form ? form.querySelector("input[name='csrf_token']") : null;
    const csrfToken = csrfInput instanceof HTMLInputElement ? csrfInput.value : "";

    if (!endpoint || !(input instanceof HTMLInputElement) || !csrfToken) {
        if (feedback instanceof HTMLElement) {
            feedback.textContent = "Workspace picker is not configured correctly.";
        }
        return;
    }

    button.disabled = true;
    if (feedback instanceof HTMLElement) {
        feedback.textContent = "Opening native workspace picker...";
    }

    try {
        const response = await fetch(endpoint, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "accept": "application/json",
                "x-csrf-token": csrfToken,
            },
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(payload.detail || payload.error || "Workspace picker failed.");
        }
        if (payload.cancelled) {
            if (feedback instanceof HTMLElement) {
                feedback.textContent = payload.message || "No workspace file was selected.";
            }
            return;
        }
        input.value = payload.filesystem_path || "";
        input.dispatchEvent(new Event("input", { bubbles: true }));
        if (feedback instanceof HTMLElement) {
            feedback.textContent = payload.message || "Workspace file selected.";
        }
    } catch (error) {
        if (feedback instanceof HTMLElement) {
            feedback.textContent = error instanceof Error ? error.message : "Workspace picker failed.";
        }
    } finally {
        button.disabled = false;
    }
});
