/** Thumbnail sheets: one frame a second, tiled by the worker. Loaded lazily
 *  and drawn straight from the sheet, so scrubbing has a picture before the
 *  video element has decoded one. */

import type { MediaInfo } from "../../types";

export interface Sheets {
  images: (HTMLImageElement | null)[];
  count: number;
  columns: number;
  perSheet: number;
  width: number;
  height: number;
  fps: number;
}

export function loadSheets(info: NonNullable<MediaInfo["thumbs"]>, onLoad: () => void): Sheets {
  const images = info.urls.map((url) => {
    const image = new Image();
    image.decoding = "async";
    image.onload = onLoad;
    image.src = url;
    return image;
  });
  return {
    images,
    count: info.count,
    columns: info.columns,
    perSheet: info.perSheet,
    width: info.width,
    height: info.height,
    fps: info.fps,
  };
}

/** Where the thumbnail for `seconds` is, or null while its sheet is loading. */
export function locate(sheets: Sheets, seconds: number) {
  const index = Math.min(sheets.count - 1, Math.max(0, Math.floor(seconds * sheets.fps)));
  const sheet = Math.floor(index / sheets.perSheet);
  const image = sheets.images[sheet];
  if (!image || !image.complete || !image.naturalWidth) return null;
  const within = index % sheets.perSheet;
  return {
    image,
    sx: (within % sheets.columns) * sheets.width,
    sy: Math.floor(within / sheets.columns) * sheets.height,
  };
}
