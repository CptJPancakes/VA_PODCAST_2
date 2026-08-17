document.addEventListener('DOMContentLoaded', () => {
  const refreshButton = document.getElementById('refresh-button');
  const refreshStatus = document.getElementById('refresh-status');
  const timeframeButtons = document.querySelectorAll('.timeframe');
  const detailButtons = document.querySelectorAll('.details-button');
  const tabs = document.querySelectorAll('.tab');
  const panels = document.querySelectorAll('.tab-panel');

  const huntModal = document.getElementById('hunt-modal');
  const huntDialog = document.getElementById('hunt-dialog');
  const huntDialogClose = document.getElementById('hunt-dialog-close');
  const huntCloseButton = document.getElementById('hunt-close-button');
  const huntViewButton = document.getElementById('hunt-view-button');
  const huntTitle = document.getElementById('hunt-title');
  const huntPhase = document.getElementById('hunt-phase');
  const huntCurrentSource = document.getElementById('hunt-current-source');
  const huntProgress = document.getElementById('hunt-progress');
  const huntProgressLabel = document.getElementById('hunt-progress-label');
  const huntProgressPercent = document.getElementById('hunt-progress-percent');
  const huntSourceCount = document.getElementById('hunt-source-count');
  const huntSucceeded = document.getElementById('hunt-succeeded');
  const huntFailed = document.getElementById('hunt-failed');
  const huntElapsed = document.getElementById('hunt-elapsed');
  const huntEta = document.getElementById('hunt-eta');
  const huntError = document.getElementById('hunt-error');

  let currentHunt = null;
  let statusReceivedAt = Date.now();
  let pollTimer = null;
  let clockTimer = null;
  let focusBeforeModal = null;

  function showRefreshStatus(message, isError = false) {
    if (!refreshStatus) {
      return;
    }
    refreshStatus.textContent = message;
    refreshStatus.classList.toggle('refresh-status--error', Boolean(isError));
    refreshStatus.hidden = !message;
  }

  function formatDuration(seconds) {
    const safeSeconds = Math.max(0, Math.round(Number(seconds) || 0));
    const hours = Math.floor(safeSeconds / 3600);
    const minutes = Math.floor((safeSeconds % 3600) / 60);
    const remainingSeconds = safeSeconds % 60;
    if (hours > 0) {
      return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(remainingSeconds).padStart(2, '0')}`;
    }
    return `${String(minutes).padStart(2, '0')}:${String(remainingSeconds).padStart(2, '0')}`;
  }

  function openHuntModal() {
    if (!huntModal || !huntDialog) {
      return;
    }
    if (huntModal.hidden) {
      focusBeforeModal = document.activeElement;
    }
    huntModal.hidden = false;
    document.body.classList.add('modal-open');
    huntDialog.focus();
  }

  function closeHuntModal() {
    if (!huntModal) {
      return;
    }
    huntModal.hidden = true;
    document.body.classList.remove('modal-open');
    if (focusBeforeModal && typeof focusBeforeModal.focus === 'function') {
      focusBeforeModal.focus();
    }
  }

  function overallProgress(status) {
    const total = Number(status.total) || 0;
    const completed = Number(status.completed) || 0;
    const sourceProgress = total > 0 ? Math.min(1, completed / total) : 0;

    if (status.status === 'succeeded') {
      return 100;
    }
    if (status.phase === 'publishing') {
      return 99;
    }
    if (status.phase === 'ranking_timeframes') {
      const frameTotal = Number(status.total_timeframes) || 5;
      const frameDone = Number(status.completed_timeframes) || 0;
      return Math.min(98, 87 + (frameDone / frameTotal) * 11);
    }
    if (status.phase === 'checking_source_health') {
      return 86;
    }
    return sourceProgress * 85;
  }

  function progressLabel(status) {
    if (status.status === 'succeeded') {
      return 'Dashboard ready';
    }
    if (status.status === 'failed') {
      return 'Hunt stopped';
    }
    if (status.phase === 'ranking_timeframes') {
      return `Ranking timeframe ${Math.min((Number(status.completed_timeframes) || 0) + 1, Number(status.total_timeframes) || 5)} of ${Number(status.total_timeframes) || 5}`;
    }
    if (status.phase === 'publishing') {
      return 'Publishing dashboard';
    }
    if (status.phase === 'checking_source_health') {
      return 'Checking source health';
    }
    if (Number(status.total) > 0) {
      return `${Number(status.completed) || 0} of ${Number(status.total)} sources checked`;
    }
    return 'Starting source checks';
  }

  function updateClock() {
    if (!currentHunt) {
      return;
    }
    const sinceStatus = currentHunt.status === 'running' ? (Date.now() - statusReceivedAt) / 1000 : 0;
    const elapsed = (Number(currentHunt.elapsed_seconds) || 0) + sinceStatus;
    if (huntElapsed) {
      huntElapsed.textContent = formatDuration(elapsed);
    }
    if (huntEta) {
      if (currentHunt.status === 'succeeded') {
        huntEta.textContent = 'Done';
      } else if (currentHunt.status === 'failed') {
        huntEta.textContent = 'Stopped';
      } else if (currentHunt.eta_seconds === null || currentHunt.eta_seconds === undefined) {
        huntEta.textContent = ['checking_source_health', 'ranking_timeframes', 'publishing'].includes(currentHunt.phase)
          ? 'Finishing up…'
          : 'Calculating…';
      } else {
        huntEta.textContent = formatDuration(Math.max(0, Number(currentHunt.eta_seconds) - sinceStatus));
      }
    }
  }

  function updateHuntDisplay(status) {
    currentHunt = status;
    statusReceivedAt = Date.now();

    const running = status.status === 'running';
    const succeeded = status.status === 'succeeded';
    const failed = status.status === 'failed';

    if (refreshButton) {
      refreshButton.textContent = running ? 'Hunt in Progress…' : 'Go Hunting';
      refreshButton.setAttribute('aria-busy', String(running));
    }
    if (huntTitle) {
      huntTitle.textContent = succeeded ? 'Hunt Complete' : failed ? 'Hunt Stopped' : 'Going Hunting';
    }
    if (huntPhase) {
      huntPhase.textContent = status.message || 'Checking local sources…';
    }

    const source = status.current_source;
    if (huntCurrentSource) {
      const sourceName = source && (source.name || source.id);
      huntCurrentSource.textContent = sourceName ? `Now checking: ${sourceName}` : '';
      huntCurrentSource.hidden = !sourceName || !running;
    }

    const percent = Math.max(0, Math.min(100, Math.round(overallProgress(status))));
    if (huntProgress) {
      huntProgress.value = percent;
      huntProgress.textContent = `${percent}%`;
      huntProgress.setAttribute('aria-valuetext', progressLabel(status));
    }
    if (huntProgressPercent) {
      huntProgressPercent.textContent = `${percent}%`;
    }
    if (huntProgressLabel) {
      huntProgressLabel.textContent = progressLabel(status);
    }
    if (huntSourceCount) {
      huntSourceCount.textContent = `${Number(status.completed) || 0} / ${Number(status.total) || '—'}`;
    }
    if (huntSucceeded) {
      huntSucceeded.textContent = String(Number(status.succeeded) || 0);
    }
    if (huntFailed) {
      huntFailed.textContent = String(Number(status.failed) || 0);
    }

    if (huntError) {
      huntError.textContent = failed
        ? `${status.error || 'The hunt could not be completed.'} Your previous dashboard is unchanged.`
        : '';
      huntError.hidden = !failed;
    }
    if (huntViewButton) {
      huntViewButton.hidden = !succeeded;
    }
    if (huntCloseButton) {
      huntCloseButton.textContent = succeeded || failed ? 'Close' : 'Keep Browsing';
    }

    if (running) {
      showRefreshStatus('Hunt in progress — click to view.', false);
    } else if (succeeded) {
      showRefreshStatus('New stories are ready.', false);
    } else if (failed) {
      showRefreshStatus('Hunt stopped. Existing dashboard unchanged.', true);
    }
    updateClock();
  }

  function schedulePoll() {
    window.clearTimeout(pollTimer);
    if (currentHunt && currentHunt.status === 'running') {
      pollTimer = window.setTimeout(() => pollHuntStatus(false), 900);
    }
  }

  async function pollHuntStatus(openIfRunning = false) {
    try {
      const response = await fetch('/api/refresh/status', { cache: 'no-store' });
      if (!response.ok) {
        throw new Error('Status request failed');
      }
      const status = await response.json();
      updateHuntDisplay(status);
      if (openIfRunning && status.status === 'running') {
        openHuntModal();
      }
      schedulePoll();
    } catch (error) {
      // A momentary status failure should not cancel a server-side hunt.
      if (currentHunt && currentHunt.status === 'running') {
        showRefreshStatus('Hunt is still running; reconnecting…', false);
        pollTimer = window.setTimeout(() => pollHuntStatus(false), 2000);
      }
    }
  }

  async function startHunt() {
    if (currentHunt && currentHunt.status === 'running') {
      openHuntModal();
      schedulePoll();
      return;
    }

    updateHuntDisplay({
      status: 'running',
      phase: 'starting',
      message: 'Preparing to hunt for new local stories…',
      total: 0,
      completed: 0,
      attempted: 0,
      succeeded: 0,
      failed: 0,
      elapsed_seconds: 0,
      eta_seconds: null,
      total_timeframes: 5,
      completed_timeframes: 0,
    });
    openHuntModal();

    try {
      const response = await fetch('/api/refresh', { method: 'POST' });
      const status = await response.json().catch(() => ({}));
      if (response.status === 202 || (response.status === 409 && status.status === 'running')) {
        updateHuntDisplay(status);
        schedulePoll();
        return;
      }
      throw new Error('Hunt request failed');
    } catch (error) {
      updateHuntDisplay({
        ...currentHunt,
        status: 'failed',
        phase: 'failed',
        message: 'The hunt could not be started.',
        error: 'The server could not start the hunt.',
        dashboard_unchanged: true,
      });
    }
  }

  refreshButton?.addEventListener('click', startHunt);
  huntDialogClose?.addEventListener('click', closeHuntModal);
  huntCloseButton?.addEventListener('click', closeHuntModal);
  huntViewButton?.addEventListener('click', () => window.location.reload());
  document.querySelector('[data-hunt-close]')?.addEventListener('click', closeHuntModal);

  document.addEventListener('keydown', (event) => {
    if (!huntModal || huntModal.hidden) {
      return;
    }
    if (event.key === 'Escape') {
      closeHuntModal();
      return;
    }
    if (event.key !== 'Tab' || !huntDialog) {
      return;
    }
    const focusable = Array.from(huntDialog.querySelectorAll('button:not([hidden]):not(:disabled), [href], [tabindex]:not([tabindex="-1"])'));
    if (focusable.length === 0) {
      event.preventDefault();
      huntDialog.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  timeframeButtons.forEach((button) => {
    button.addEventListener('click', () => {
      const url = new URL(window.location.href);
      url.searchParams.set('range', button.dataset.range);
      window.location.assign(url.toString());
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

  clockTimer = window.setInterval(updateClock, 1000);
  window.addEventListener('beforeunload', () => {
    window.clearTimeout(pollTimer);
    window.clearInterval(clockTimer);
  });

  // This is status-only. App startup and page load never trigger a crawl.
  pollHuntStatus(true);
});
