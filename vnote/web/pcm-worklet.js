/* vnote live capture — microphone Float32 -> s16le 16 kHz mono, in ~250 ms batches.
 *
 * Runs in AudioWorkletGlobalScope: no `document`, no `window`, no fetch. The page
 * asks for an AudioContext at 16 kHz but a browser may hand back 44.1/48 kHz, so
 * `sampleRate` (the global, i.e. the *real* context rate) is resampled here with
 * linear interpolation; the fractional read position carries across process() calls
 * so no sample boundary is lost. Every channel is averaged into the one we send.
 *
 * Two kinds of message go to the page: a full batch is a bare ArrayBuffer, while
 * the reply to `{"type": "flush"}` is `{type: 'flush', buffer}` — the page's Stop
 * waits for that reply, and the tag keeps a batch that crosses it from passing as
 * one.
 */

'use strict';

const TARGET_RATE = 16000;
const BATCH = 4000;            // ~250 ms at 16 kHz

class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / TARGET_RATE;   // input samples per output sample
    this.pos = 0;        // read position inside the current block; may be < 0 (into `prev`)
    this.prev = 0;       // the previous block's last sample
    this.batch = new Int16Array(BATCH);
    this.n = 0;
    this.mix = null;     // scratch buffer for the downmix of a multi-channel input
    this.port.onmessage = (ev) => {
      if (ev && ev.data && ev.data.type === 'flush') this.flush();
    };
  }

  /* A full batch, on its own. */
  emit() {
    const out = this.batch.slice(0, this.n);   // a copy; `this.batch` stays ours
    this.n = 0;
    this.port.postMessage(out.buffer, [out.buffer]);
  }

  /* Always posts, even when empty: the page's Stop waits for this reply. */
  flush() {
    const out = this.batch.slice(0, this.n);
    this.n = 0;
    this.port.postMessage({ type: 'flush', buffer: out.buffer }, [out.buffer]);
  }

  /* One channel out of however many came in, averaged. Mono passes straight
   * through; anything wider is mixed into a reused scratch buffer. */
  downmix(input, len) {
    const n = input.length;
    if (n === 1) return input[0];
    let mix = this.mix;
    if (!mix || mix.length !== len) mix = this.mix = new Float32Array(len);
    for (let i = 0; i < len; i++) {
      let s = 0;
      for (let c = 0; c < n; c++) {
        const ch = input[c];
        s += (ch && ch.length === len) ? ch[i] : 0;
      }
      mix[i] = s / n;
    }
    return mix;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input.length) return true;
    if (!input[0] || !input[0].length) return true;
    const ch = this.downmix(input, input[0].length);

    const len = ch.length;
    const ratio = this.ratio;
    let pos = this.pos;
    while (pos < len) {
      const i = Math.floor(pos);
      if (i + 1 >= len) break;               // the interpolation needs the next block
      const a = i < 0 ? this.prev : ch[i];
      const s = a + (ch[i + 1] - a) * (pos - i);
      let v = Math.round(s * 32768);
      if (v > 32767) v = 32767; else if (v < -32768) v = -32768;
      this.batch[this.n++] = v;
      if (this.n === BATCH) this.emit();
      pos += ratio;
    }
    this.pos = pos - len;
    this.prev = ch[len - 1];
    return true;
  }
}

registerProcessor('pcm-capture', PcmCapture);
