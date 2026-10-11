import fs from 'node:fs/promises';
import path from 'node:path';
import { launchBrowser } from './browser.mjs';

const directory = path.resolve(import.meta.dirname, '../public');
const svg = await fs.readFile(path.join(directory, 'icon.svg'), 'utf8');
const browser = await launchBrowser();
try {
  const page = await browser.newPage();
  for (const [name, size] of [
    ['apple-touch-icon.png', 180],
    ['icon-192.png', 192],
    ['icon-512.png', 512],
  ]) {
    const base64 = await page.evaluate(
      async ({ svg, size }) => {
        const image = new Image();
        image.src = 'data:image/svg+xml;base64,' + btoa(svg);
        await image.decode();
        const canvas = document.createElement('canvas');
        canvas.width = canvas.height = size;
        const ctx = canvas.getContext('2d');
        ctx.fillStyle = '#fb637a';
        ctx.fillRect(0, 0, size, size);
        ctx.drawImage(image, 0, 0, size, size);
        return canvas.toDataURL('image/png').split(',')[1];
      },
      { svg, size },
    );
    await fs.writeFile(path.join(directory, name), Buffer.from(base64, 'base64'));
  }
} finally {
  await browser.close();
}
