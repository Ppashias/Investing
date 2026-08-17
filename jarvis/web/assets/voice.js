/* Voice, using the browser's own speech engine.
 *
 * ## Why here and not in Python
 *
 * The obvious build is a Python pipeline — VAD, faster-whisper, a TTS model —
 * which is what ONEPUNCHMAN411/Jarvis does. It is also a model download, a set
 * of native wheels, and a microphone permission held by a long-running daemon.
 * The browser already has SpeechRecognition and speechSynthesis, they need no
 * dependency, no build step and no CDN, and the microphone permission is
 * granted per-origin by the user and revocable in one click. For a UI that is
 * already a browser page, that is a better trade.
 *
 * ## The privacy cost, stated rather than buried
 *
 * Chrome's SpeechRecognition sends audio to Google for transcription. That
 * directly contradicts this system's local-first proposition, so voice input is
 * OFF by default, the toggle says where the audio goes, and nothing here starts
 * a microphone without a click. Speech *output* is local synthesis and carries
 * no such cost, so the two are separate switches.
 *
 * ## What voice may and may not do
 *
 * It may dictate a message and read a reply. It may answer a confirmation —
 * and the server refuses a spoken approval of anything destructive, which was
 * built in the previous commit precisely so this one could not be the thing
 * that introduced the hole. The client says `channel: "voice"` honestly; the
 * rule does not depend on it doing so, because the server decides.
 */
(() => {
  "use strict";

  const Recognition =
    window.SpeechRecognition || window.webkitSpeechRecognition || null;
  const synth = window.speechSynthesis || null;

  const bar = document.getElementById("voiceBar");
  if (!bar) return;

  const micBtn = document.getElementById("micBtn");
  const speakBtn = document.getElementById("speakBtn");
  const status = document.getElementById("voiceStatus");
  const input = document.getElementById("input");
  const composer = document.getElementById("composer");

  const SPEAK_KEY = "jarvis.voice.speak";
  let listening = false;
  let recogniser = null;

  function say(text) {
    if (status) status.textContent = text;
  }

  /* ── availability ─────────────────────────────────────────────────────── */

  if (!Recognition && micBtn) {
    micBtn.disabled = true;
    micBtn.title = "This browser has no speech recognition.";
  }
  if (!synth && speakBtn) {
    speakBtn.disabled = true;
    speakBtn.title = "This browser has no speech synthesis.";
  }
  if (!Recognition && !synth) {
    say("voice unavailable in this browser");
    return;
  }

  /* ── output ───────────────────────────────────────────────────────────── */

  // Persisted, because being read aloud is a preference rather than a session
  // decision — and defaulting to off, because a machine that starts talking
  // unprompted is a machine people turn off entirely.
  let speaking = localStorage.getItem(SPEAK_KEY) === "1";

  function paintSpeak() {
    if (!speakBtn) return;
    speakBtn.textContent = speaking ? "Voice on" : "Voice off";
    speakBtn.classList.toggle("on", speaking);
    speakBtn.setAttribute("aria-pressed", speaking ? "true" : "false");
  }
  paintSpeak();

  if (speakBtn) {
    speakBtn.addEventListener("click", () => {
      speaking = !speaking;
      localStorage.setItem(SPEAK_KEY, speaking ? "1" : "0");
      if (!speaking && synth) synth.cancel();
      paintSpeak();
    });
  }

  /* Read a reply aloud. Capped, because a long answer read in full is a
     minute of speech nobody can skim and the screen already has it. */
  function speak(text) {
    if (!speaking || !synth || !text) return;
    const utterance = new SpeechSynthesisUtterance(String(text).slice(0, 700));
    utterance.rate = 1.05;
    utterance.pitch = 0.95;
    synth.cancel();
    synth.speak(utterance);
  }

  // app.js dispatches this when an assistant turn lands. A custom event rather
  // than a shared function keeps the two files independent: app.js does not
  // know whether anything is listening, and voice failing cannot break a reply
  // from rendering.
  document.addEventListener("jarvis:reply", (event) => {
    speak(event.detail && event.detail.text);
  });

  /* ── input ────────────────────────────────────────────────────────────── */

  function stop() {
    listening = false;
    if (recogniser) {
      try { recogniser.stop(); } catch (_) { /* already stopped */ }
    }
    if (micBtn) {
      micBtn.classList.remove("on");
      micBtn.setAttribute("aria-pressed", "false");
    }
    say("");
  }

  function start() {
    if (!Recognition || listening) return;
    recogniser = new Recognition();
    recogniser.lang = navigator.language || "en-GB";
    recogniser.interimResults = true;
    recogniser.continuous = false;

    recogniser.onstart = () => {
      listening = true;
      if (micBtn) {
        micBtn.classList.add("on");
        micBtn.setAttribute("aria-pressed", "true");
      }
      say("listening — audio leaves this machine for transcription");
    };

    recogniser.onresult = (event) => {
      let text = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {
        text += event.results[i][0].transcript;
      }
      if (!input) return;
      input.value = text;
      input.dispatchEvent(new Event("input", { bubbles: true }));

      // A final result fills the box. It does NOT send: dictation mishears,
      // and a message that sends itself gives nobody the chance to notice.
      // The user presses Enter, which is one keystroke and the whole
      // difference between a draft and an action.
      if (event.results[event.results.length - 1].isFinal) {
        say("dictated — check it, then send");
        stop();
        input.focus();
      }
    };

    recogniser.onerror = (event) => {
      // Named rather than generic: "not-allowed" means the permission was
      // refused and is fixed in browser settings, which "voice failed" does
      // not tell anyone.
      const reason = event && event.error ? String(event.error) : "unknown";
      say(
        reason === "not-allowed"
          ? "microphone blocked — allow it in the browser's site settings"
          : "voice input failed: " + reason
      );
      stop();
    };

    recogniser.onend = () => { if (listening) stop(); };

    try {
      recogniser.start();
    } catch (error) {
      say("could not start the microphone");
      stop();
    }
  }

  if (micBtn) {
    micBtn.addEventListener("click", () => (listening ? stop() : start()));
  }

  // Stop listening the moment a message is sent, so the microphone is never
  // live while JARVIS is answering — a hot mic during a reply is how a spoken
  // "yes" meant for a person becomes an approval.
  if (composer) composer.addEventListener("submit", stop);

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stop();
      if (synth) synth.cancel();
    }
  });
})();
