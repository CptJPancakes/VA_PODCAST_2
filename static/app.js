document.addEventListener('DOMContentLoaded', () => {
  const refreshButton = document.getElementById('refresh-button');
  const refreshStatus = document.getElementById('refresh-status');
  const timeframeButtons = document.querySelectorAll('.timeframe');
  const detailButtons = document.querySelectorAll('.details-button');
  const tabs = document.querySelectorAll('.tab');
  const panels = document.querySelectorAll('.tab-panel');

  function showRefreshStatus(message, isError) {
    if (!refreshStatus) {
      return;
    }
    refreshStatus.textContent = message;
    refreshStatus.classList.toggle('refresh-status--error', Boolean(isError));
    refreshStatus.hidden = false;
  }

  refreshButton?.addEventListener('click', async () => {
    refreshButton.disabled = true;
    refreshButton.textContent = 'Refreshing…';
    if (refreshStatus) {
      refreshStatus.hidden = true;
    }

    try {
      const response = await fetch('/api/refresh', { method: 'POST' });

      if (response.status === 409) {
        showRefreshStatus('Refresh already in progress.', false);
        refreshButton.disabled = false;
        refreshButton.textContent = 'Refresh';
        return;
      }

      const result = await response.json().catch(() => ({}));

      if (response.ok && result.success) {
        // Get the newest evidence, then reload to render it — server-rendering
        // already works, so reuse it instead of syncing data client-side.
        window.location.reload();
        return;
      }

      showRefreshStatus('Refresh failed. Existing dashboard data is unchanged.', true);
    } catch (error) {
      showRefreshStatus('Refresh failed. Existing dashboard data is unchanged.', true);
    } finally {
      refreshButton.disabled = false;
      refreshButton.textContent = 'Refresh';
    }
  });

  timeframeButtons.forEach((button) => {
    button.addEventListener('click', () => {
      timeframeButtons.forEach((item) => item.classList.remove('active'));
      button.classList.add('active');
    });
  });

  tabs.forEach((button) => {
    button.addEventListener('click', () => {
      const target = button.dataset.target;

      tabs.forEach((tab) => {
        tab.classList.toggle('active', tab === button);
        tab.setAttribute('aria-selected', String(tab === button));
      });

      panels.forEach((panel) => {
        panel.classList.toggle('active', panel.dataset.panel === target);
      });
    });
  });

  detailButtons.forEach((button) => {
    button.addEventListener('click', () => {
      const details = button.parentElement.querySelector('.topic-details');
      if (!details) {
        return;
      }

      const isHidden = details.hasAttribute('hidden');
      if (isHidden) {
        details.removeAttribute('hidden');
        button.textContent = 'Hide Topic';
      } else {
        details.setAttribute('hidden', 'hidden');
        button.textContent = 'View Topic';
      }
    });
  });
});
