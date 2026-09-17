/** The waveform: one byte per hundredth of a second, fetched once, folded
 *  into pixel columns at whatever zoom the timeline is at. */

export interface Peaks {
  data: Uint8Array;
  rate: number;
}

export async function loadPeaks(url: string, rate: number): Promise<Peaks> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`forma d'onda non disponibile (${response.status})`);
  return { data: new Uint8Array(await response.arrayBuffer()), rate };
}

/** The loudest value between two instants, 0–1. */
export function peakBetween(peaks: Peaks, from: number, to: number): number {
  const first = Math.max(0, Math.floor(from * peaks.rate));
  const last = Math.min(peaks.data.length - 1, Math.ceil(to * peaks.rate));
  let loudest = 0;
  for (let index = first; index <= last; index += 1) {
    if (peaks.data[index] > loudest) loudest = peaks.data[index];
  }
  return loudest / 255;
}
