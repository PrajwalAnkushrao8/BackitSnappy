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

function wireWizardStep(stepName, inputIds, submitFn) {
  const btn = document.getElementById(`btn-submit-${stepName}`);
  const errorEl = document.getElementById(`error-${stepName}`);
  btn.addEventListener('click', async () => {
    errorEl.textContent = '';
    btn.disabled = true;
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
    }
  });
}

function initSetupWizard() {
  document.getElementById('btn-open-telegram-api').addEventListener('click', () => {
    window.pywebview.api.open_telegram_api_page();
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
