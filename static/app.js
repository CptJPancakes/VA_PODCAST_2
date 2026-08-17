document.addEventListener('DOMContentLoaded', () => {
  const refreshButton = document.getElementById('refresh-button');
  const timeframeButtons = document.querySelectorAll('.timeframe');
  const detailButtons = document.querySelectorAll('.details-button');
  const tabs = document.querySelectorAll('.tab');
  const panels = document.querySelectorAll('.tab-panel');

  refreshButton?.addEventListener('click', () => {
    window.location.reload();
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
