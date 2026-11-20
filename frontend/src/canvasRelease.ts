/**
 * Drop a temporary canvas's backing store right away. iPhone Safari caps total canvas
 * memory, and detached canvases are reclaimed late, so every export path releases its
 * scratch canvases explicitly once their pixels have been read.
 */
export function releaseCanvas(...canvases: (HTMLCanvasElement | null | undefined)[]) {
  for (const canvas of canvases) {
    if (!canvas) continue;
    canvas.width = 0;
    canvas.height = 0;
  }
}
