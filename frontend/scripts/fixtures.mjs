import { deflateSync } from 'node:zlib';

// Original synthetic artwork: CI must not read private catalogs or game assets.
export const jacketSVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="#48abd5"/><path d="M0 80 80 0v80Z" fill="#d74783"/></svg>';
export const badgeSVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><circle cx="39.5" cy="39.5" r="35" fill="#ffd965"/><circle cx="39.5" cy="39.5" r="24" fill="#ec569e"/><path d="m20 39 12 12 26-28" fill="none" stroke="white" stroke-width="7"/></svg>';

function pngChunk(type, body) {
  const data = Buffer.concat([Buffer.from(type), body]);
  let crc = 0xffffffff;
  for (const byte of data) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
  }
  const header = Buffer.alloc(4),
    checksum = Buffer.alloc(4);
  header.writeUInt32BE(body.length);
  checksum.writeUInt32BE((crc ^ 0xffffffff) >>> 0);
  return Buffer.concat([header, data, checksum]);
}

function makeBadgePNG() {
  const size = 80,
    stride = 1 + size * 4;
  const pixels = Buffer.alloc(size * stride);
  for (let y = 0; y < size; y++)
    for (let x = 0; x < size; x++) {
      const distance = Math.hypot(x - 39.5, y - 39.5);
      const color =
        distance < 24 ? [236, 86, 158, 255] : distance < 35 ? [255, 217, 101, 255] : [0, 0, 0, 0];
      pixels.set(color, y * stride + 1 + x * 4);
    }
  const header = Buffer.alloc(13);
  header.writeUInt32BE(size, 0);
  header.writeUInt32BE(size, 4);
  header[8] = 8;
  header[9] = 6; // RGBA, eight bits per channel, filter 0.
  return Buffer.concat([
    Buffer.from('89504e470d0a1a0a', 'hex'),
    pngChunk('IHDR', header),
    pngChunk('IDAT', deflateSync(pixels)),
    pngChunk('IEND', Buffer.alloc(0)),
  ]);
}

export const badgePNG = makeBadgePNG();

export function fixtureAsset(pathname) {
  if (pathname.startsWith('/jackets/')) return { contentType: 'image/svg+xml', body: jacketSVG };
  if (pathname === '/cover-assets/version-backgrounds/manifest.json') {
    const version_map = Object.fromEntries(
      ['UNiVERSE', 'FESTiVAL'].map((version) => [
        version,
        {
          family_key: version,
          version_name: version,
          logo_file: 'logo.svg',
          background_png: 'background.svg',
          status: 'ready',
        },
      ]),
    );
    return { contentType: 'application/json', body: JSON.stringify({ version_map }) };
  }
  if (pathname === '/cover-assets/game-ui/manifest.json') {
    return {
      contentType: 'application/json',
      body: JSON.stringify({
        assets: Object.fromEntries(
          ['dx', 'standard', 'ap', 'app'].map((key) => [key, { file: 'badge.svg' }]),
        ),
        number_sets: {},
      }),
    };
  }
  if (pathname.startsWith('/cover-assets/'))
    return {
      contentType: 'image/svg+xml',
      body: pathname.includes('background') ? jacketSVG : badgeSVG,
    };
  return null;
}

export async function useSystemCoverFont(context) {
  await context.addInitScript(() => {
    const NativeFontFace = window.FontFace;
    window.FontFace = class extends NativeFontFace {
      constructor(family, source, descriptors) {
        super(
          family,
          family === 'CoverNewRodin'
            ? 'local("Arial"), local("Liberation Sans"), local("Helvetica")'
            : source,
          descriptors,
        );
      }
    };
  });
}
