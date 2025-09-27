(function () {
    const audioList = document.getElementById('audio-list');
    const audio = document.getElementById('audio-player');
    const trackTitle = document.getElementById('track-title');
    const resumeInfo = document.getElementById('resume-info');
    const playbackControls = document.getElementById('playback-controls');

    if (!audio || !trackTitle) {
        return;
    }

    let currentFile = null;
    let currentButton = null;
    let pendingResume = 0;
    let lastSync = 0;
    let playbackRate = 1;
    let redirectingToLogin = false;
    const SYNC_INTERVAL_MS = 5000;

    function formatTime(seconds) {
        if (!Number.isFinite(seconds) || seconds <= 0) {
            return '0:00';
        }
        const total = Math.round(seconds);
        const mins = Math.floor(total / 60);
        const secs = String(total % 60).padStart(2, '0');
        return `${mins}:${secs}`;
    }

    function setResumeMessage(position, duration) {
        if (position > 0 && Number.isFinite(duration) && duration > 0) {
            resumeInfo.textContent = `Resume from ${formatTime(position)} of ${formatTime(duration)}`;
        } else if (position > 0) {
            resumeInfo.textContent = `Resume from ${formatTime(position)}`;
        } else {
            resumeInfo.textContent = '';
        }
    }

    function updateProgressDisplay() {
        if (!Number.isFinite(audio.duration) || audio.duration <= 0) {
            return;
        }
        resumeInfo.textContent = `Progress ${formatTime(audio.currentTime)} / ${formatTime(audio.duration)}`;
    }

    function redirectToLogin() {
        if (redirectingToLogin) {
            return;
        }
        redirectingToLogin = true;
        const returnUrl = `${window.location.pathname}${window.location.search}`;
        window.location.href = `/login?next=${encodeURIComponent(returnUrl)}`;
    }

    async function fetchProgress(file) {
        try {
            const response = await fetch(`/api/progress?file=${encodeURIComponent(file)}`);
            if (response.status === 401) {
                redirectToLogin();
                return { position: 0, duration: 0 };
            }
            if (!response.ok) {
                return { position: 0, duration: 0 };
            }
            return await response.json();
        } catch (error) {
            console.error('Failed to load progress', error);
            return { position: 0, duration: 0 };
        }
    }

    function saveProgressPayload(position, duration) {
        return JSON.stringify({
            file: currentFile,
            position,
            duration,
        });
    }

    async function syncProgress(force = false, keepalive = false) {
        if (!currentFile) {
            return;
        }
        const now = Date.now();
        if (!force && now - lastSync < SYNC_INTERVAL_MS) {
            return;
        }
        lastSync = now;

        const duration = Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : 0;
        const position = Number.isFinite(audio.currentTime) ? audio.currentTime : 0;
        const body = saveProgressPayload(position, duration);

        if (keepalive && navigator.sendBeacon) {
            const blob = new Blob([body], { type: 'application/json' });
            navigator.sendBeacon('/api/progress', blob);
            return;
        }

        try {
            const response = await fetch('/api/progress', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body,
                keepalive,
            });
            if (response && response.status === 401) {
                redirectToLogin();
            }
        } catch (error) {
            console.error('Failed to sync progress', error);
        }
    }

    function setActiveButton(button) {
        if (currentButton) {
            currentButton.classList.remove('active');
        }
        currentButton = button;
        currentButton.classList.add('active');
    }

    function setActiveRateButton(rate) {
        if (!playbackControls) {
            return;
        }
        const buttons = playbackControls.querySelectorAll('[data-rate]');
        buttons.forEach((btn) => {
            const value = parseFloat(btn.dataset.rate);
            if (value === rate) {
                btn.classList.add('active');
            } else {
                btn.classList.remove('active');
            }
        });
    }

    function applyPlaybackRate(rate, { persist = true } = {}) {
        if (!Number.isFinite(rate) || rate <= 0) {
            return;
        }
        playbackRate = rate;
        audio.playbackRate = rate;
        setActiveRateButton(rate);
        if (persist && window.localStorage) {
            try {
                window.localStorage.setItem('player.playbackRate', String(rate));
            } catch (error) {
                console.warn('Unable to persist playback rate', error);
            }
        }
    }

    async function loadTrack(button) {
        const file = button.dataset.file;
        const name = button.dataset.name || file;
        if (!file) {
            return;
        }
        audio.pause();
        if (currentFile) {
            await syncProgress(true);
        }
        setActiveButton(button);
        currentFile = file;
        trackTitle.textContent = name;
        resumeInfo.textContent = 'Loading progress...';
        pendingResume = 0;
        audio.src = `/media?path=${encodeURIComponent(file)}`;
        audio.load();
        audio.playbackRate = playbackRate;

        const progress = await fetchProgress(file);
        pendingResume = progress.position || 0;
        setResumeMessage(pendingResume, progress.duration || 0);
    }

    if (audioList) {
        audioList.addEventListener('click', (event) => {
            const button = event.target.closest('button[data-file]');
            if (!button) {
                return;
            }
            event.preventDefault();
            loadTrack(button);
        });
    }

    audio.addEventListener('loadedmetadata', () => {
        if (pendingResume > 0 && pendingResume < audio.duration) {
            audio.currentTime = pendingResume;
            setResumeMessage(pendingResume, audio.duration);
        } else {
            updateProgressDisplay();
        }
    });

    audio.addEventListener('timeupdate', () => {
        updateProgressDisplay();
        syncProgress();
    });

    audio.addEventListener('pause', () => {
        syncProgress(true);
    });

    audio.addEventListener('ended', () => {
        resumeInfo.textContent = 'Playback finished';
        syncProgress(true);
    });

    window.addEventListener('beforeunload', () => {
        syncProgress(true, true);
    });

    if (playbackControls) {
        playbackControls.addEventListener('click', (event) => {
            const button = event.target.closest('button[data-rate]');
            if (!button) {
                return;
            }
            event.preventDefault();
            const rate = parseFloat(button.dataset.rate);
            applyPlaybackRate(rate);
        });
    }

    if (window.localStorage) {
        const savedRate = parseFloat(window.localStorage.getItem('player.playbackRate'));
        if (Number.isFinite(savedRate) && savedRate > 0) {
            playbackRate = savedRate;
        }
    }

    applyPlaybackRate(playbackRate, { persist: false });
})();
