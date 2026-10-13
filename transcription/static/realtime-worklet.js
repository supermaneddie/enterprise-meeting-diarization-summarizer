// Batch PCM on the audio thread; flush acknowledges all audio preceding pause.
class MicrophoneCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(1600);
    this.length = 0;
    this.recording = false;
    this.port.onmessage = ({data}) => {
      if (data === 'start') this.recording = true;
      if (data === 'pause') {
        this.recording = false;
        this.flush();
        this.port.postMessage({paused: true});
      }
    };
  }
  flush() {
    if (this.length) this.port.postMessage({samples: this.buffer.slice(0, this.length)});
    this.length = 0;
  }
  process(inputs) {
    const channel = inputs[0]?.[0];
    if (this.recording && channel) {
      for (const value of channel) {
        this.buffer[this.length++] = value;
        if (this.length === this.buffer.length) this.flush();
      }
    }
    return true;
  }
}
registerProcessor('microphone-capture', MicrophoneCapture);
