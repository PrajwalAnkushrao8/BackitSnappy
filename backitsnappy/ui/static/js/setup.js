// First-run auth wizard: credentials -> phone -> code -> (optional) password.

const WIZARD_STEPS = ['credentials', 'phone', 'code', 'password'];

function showWizardStep(state) {
  const stepByState = {
    needs_credentials: 'credentials',
    needs_phone: 'phone',
    needs_code: 'code',
    needs_password: 'password',
  };
  const step = stepByState[state];
  WIZARD_STEPS.forEach((s) => {
    document.getElementById(`wizard-step-${s}`).classList.toggle('hidden', s !== step);
  });
}

// code/password submission can trigger a full local-index rebuild after a
// fresh login (see client_manager._post_auth_setup) -- worth calling out
// explicitly so a slow-but-working request doesn't look identical to a
// hung one.
const WIZARD_LOADING_TEXT = {
  credentials: 'Continuing…',
  phone: 'Sending…',
  code: 'Verifying… this can take a minute on first login',
  password: 'Verifying… this can take a minute on first login',
};

function wireWizardStep(stepName, inputIds, submitFn) {
  const btn = document.getElementById(`btn-submit-${stepName}`);
  const errorEl = document.getElementById(`error-${stepName}`);
  const originalLabel = btn.textContent;
  btn.addEventListener('click', async () => {
    errorEl.textContent = '';
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner"></span>${WIZARD_LOADING_TEXT[stepName] || 'Working…'}`;
    try {
      const values = inputIds.map((id) => document.getElementById(id).value.trim());
      const { state } = await submitFn(...values);
      if (state === 'authorized') {
        await checkSetupStatus();
      } else {
        showWizardStep(state);
      }
    } catch (e) {
      errorEl.textContent = e.message;
    } finally {
      btn.disabled = false;
      btn.textContent = originalLabel;
    }
  });
}

function initSetupWizard() {
  document.getElementById('btn-open-telegram-api').addEventListener('click', () => {
    window.pywebview.api.open_telegram_api_page();
  });

  // Purely client-side navigation -- the credentials step only shows up
  // after submitting a phone number that turned out to have no api_id/
  // api_hash bound yet (see client_manager.send_code), and nothing on the
  // backend has been created for it at that point (no client, no code
  // request), so there's nothing to undo server-side. Re-submitting a
  // (possibly different) phone number from here works exactly the same
  // as submitting it the first time.
  document.getElementById('btn-back-credentials').addEventListener('click', () => {
    document.getElementById('error-credentials').textContent = '';
    showWizardStep('needs_phone');
  });

  wireWizardStep('credentials', ['input-api-id', 'input-api-hash'], (apiId, apiHash) =>
    API.request('/api/setup/credentials', { method: 'POST', json: { api_id: Number(apiId), api_hash: apiHash } })
  );
  wireWizardStep('phone', ['input-phone'], (phone) =>
    API.request('/api/setup/phone', { method: 'POST', json: { phone } })
  );
  wireWizardStep('code', ['input-code'], (code) =>
    API.request('/api/setup/code', { method: 'POST', json: { code } })
  );
  wireWizardStep('password', ['input-password'], (password) =>
    API.request('/api/setup/password', { method: 'POST', json: { password } })
  );
}

initSetupWizard();
