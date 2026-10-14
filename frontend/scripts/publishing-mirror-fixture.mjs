import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';

export async function capturePublishingMirrorInputs(page) {
  await page.evaluate(() => {
    const nativeDrawImage = CanvasRenderingContext2D.prototype.drawImage;
    const capture = { nativeDrawImage, frames: [] };
    window.__publishingMirrorCapture = capture;
    CanvasRenderingContext2D.prototype.drawImage = function (source, ...args) {
      const transform = this.getTransform();
      const fullCover =
        source instanceof HTMLCanvasElement &&
        [1920, 1440].includes(source.width) &&
        source.height === 1080;
      if (fullCover && transform.a < 0)
        capture.frames.push({
          width: source.width,
          height: source.height,
          image: source.toDataURL('image/png'),
        });
      return nativeDrawImage.call(this, source, ...args);
    };
  });
}

export async function verifyPublishingMirrors(page, output, viewportWidth) {
  const comparisons = await page.evaluate(async () => {
    const capture = window.__publishingMirrorCapture;
    CanvasRenderingContext2D.prototype.drawImage = capture.nativeDrawImage;
    delete window.__publishingMirrorCapture;
    async function pixels(src) {
      const image = new Image();
      image.src = src;
      await image.decode();
      const canvas = document.createElement('canvas');
      canvas.width = image.naturalWidth;
      canvas.height = image.naturalHeight;
      const context = canvas.getContext('2d');
      context.drawImage(image, 0, 0);
      return {
        width: canvas.width,
        height: canvas.height,
        data: context.getImageData(0, 0, canvas.width, canvas.height).data,
      };
    }
    const results = [];
    for (const frame of capture.frames) {
      const aspect = frame.width === 1920 ? '16:9' : '4:3';
      const after = document.querySelector(`img[alt="${aspect} 投稿封面预览"]`).src;
      const original = await pixels(frame.image);
      const mirrored = await pixels(after);
      const differences = [];
      let differentPixels = 0;
      for (let y = 0; y < original.height; y++)
        for (let x = 0; x < original.width; x++) {
          const beforeIndex = (y * original.width + x) * 4;
          const afterIndex = (y * mirrored.width + mirrored.width - 1 - x) * 4;
          if (
            original.data[beforeIndex] !== mirrored.data[afterIndex] ||
            original.data[beforeIndex + 1] !== mirrored.data[afterIndex + 1] ||
            original.data[beforeIndex + 2] !== mirrored.data[afterIndex + 2] ||
            original.data[beforeIndex + 3] !== mirrored.data[afterIndex + 3]
          ) {
            differentPixels += 1;
            if (differences.length < 8)
              differences.push({
                x,
                y,
                before: [...original.data.slice(beforeIndex, beforeIndex + 4)],
                after: [...mirrored.data.slice(afterIndex, afterIndex + 4)],
              });
          }
        }
      results.push({
        aspect,
        original: [original.width, original.height],
        mirrored: [mirrored.width, mirrored.height],
        differentPixels,
        differences,
        before: frame.image,
        after,
      });
    }
    return results;
  });
  assert.deepEqual(
    comparisons.map((comparison) => comparison.original),
    [
      [1920, 1080],
      [1440, 1080],
    ],
    'Both ratios must mirror the entire rendered canvas, not only a jacket or badge',
  );
  for (const { before, after, ...comparison } of comparisons) {
    assert.deepEqual(comparison.mirrored, comparison.original);
    if (comparison.differentPixels) {
      const prefix = path.join(output, `mirror-${viewportWidth}-${comparison.original[0]}`);
      await Promise.all([
        fs.writeFile(`${prefix}-before.png`, Buffer.from(before.split(',')[1], 'base64')),
        fs.writeFile(`${prefix}-after.png`, Buffer.from(after.split(',')[1], 'base64')),
      ]);
    }
    assert.equal(
      comparison.differentPixels,
      0,
      `The entire rendered cover must mirror exactly: ${JSON.stringify(comparison)}`,
    );
  }
}
