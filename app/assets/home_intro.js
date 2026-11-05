(() => {
  const video = document.getElementById("sec-home-intro-video");
  if (!video) return;
  window.__secIntroCleanup?.();

  const controller = new AbortController();
  const { signal } = controller;
  const timers = [];
  let observer;
  let frame;
  let pending = false;
  const cleanup = () => {
    controller.abort();
    observer?.disconnect();
    cancelAnimationFrame(frame);
    timers.forEach(clearTimeout);
    if (window.__secIntroCleanup === cleanup) delete window.__secIntroCleanup;
  };
  window.__secIntroCleanup = cleanup;

  // Set live properties before assigning src. Attributes alone do not restart
  // playback when Streamlit mounts the element or Safari delays its first load.
  video.defaultMuted = true;
  video.muted = true;
  video.volume = 0;
  video.playsInline = true;
  video.autoplay = true;
  video.loop = true;
  video.controls = false;
  video.disablePictureInPicture = true;
  video.disableRemotePlayback = true;

  const play = () => {
    if (!video.isConnected) return cleanup();
    if (document.hidden || !video.paused || pending) return;
    pending = true;
    // A browser's explicit autoplay restriction must not produce an unhandled
    // rejection. Readiness/visibility events provide subsequent opportunities.
    Promise.resolve(video.play()).catch(() => {}).finally(() => { pending = false; });
  };
  for (const event of ["loadedmetadata", "canplay", "canplaythrough"])
    video.addEventListener(event, play, { signal });
  document.addEventListener("visibilitychange", play, { signal });
  video.parentElement.addEventListener("contextmenu", event => event.preventDefault(), { signal });
  if (typeof IntersectionObserver !== "undefined") {
    observer = new IntersectionObserver(entries => {
      if (!video.isConnected) cleanup();
      else if (entries.some(entry => entry.isIntersecting)) play();
    });
    observer.observe(video);
  }

  video.src = video.dataset.src;
  video.removeAttribute("data-src");
  video.load();
  frame = requestAnimationFrame(play);
  for (const delay of [300, 1200]) timers.push(setTimeout(play, delay));
})();
