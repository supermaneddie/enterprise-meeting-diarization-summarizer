// Web Audio resamples the microphone to 16 kHz before the worklet captures PCM.
export function pcm16(samples) {
  const bytes = new Uint8Array(samples.length * 2);
  const view = new DataView(bytes.buffer);
  samples.forEach((value, index) => {
    const sample = Math.max(-1, Math.min(1, value));
    view.setInt16(index * 2, sample * (sample < 0 ? 32768 : 32767), true);
  });
  return bytes;
}

export function wavBlob(parts, count, rate = 16000) {
  const header = new ArrayBuffer(44), view = new DataView(header);
  const text = (offset, value) => [...value].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)));
  text(0, 'RIFF'); view.setUint32(4, 36 + count * 2, true); text(8, 'WAVE');
  text(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, 1, true); view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  text(36, 'data'); view.setUint32(40, count * 2, true);
  return new Blob([header, ...parts], {type: 'audio/wav'});
}
