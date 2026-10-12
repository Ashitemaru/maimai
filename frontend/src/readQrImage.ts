export async function readQrImage(file: File): Promise<string> {
  if (!file.type.startsWith('image/')) throw new Error('请选择二维码截图');
  if (file.size > 20 * 1024 * 1024) throw new Error('图片过大，请选择 20 MB 以内的截图');
  const { default: jsQR } = await import('jsqr');
  const url = URL.createObjectURL(file);
  const image = new Image();
  try {
    image.src = url;
    await image.decode();
    if (!image.naturalWidth || !image.naturalHeight) throw new Error('无法读取图片');
    const canvas = document.createElement('canvas');
    const context = canvas.getContext('2d', { willReadFrequently: true });
    if (!context) throw new Error('此浏览器无法读取图片，请粘贴二维码内容');
    try {
      for (const limit of [1600, 2600]) {
        const scale = Math.min(1, limit / Math.max(image.naturalWidth, image.naturalHeight));
        canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
        canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
        context.drawImage(image, 0, 0, canvas.width, canvas.height);
        const pixels = context.getImageData(0, 0, canvas.width, canvas.height);
        const decoded = jsQR(pixels.data, pixels.width, pixels.height, {
          inversionAttempts: 'attemptBoth',
        });
        if (decoded?.data) return decoded.data;
        if (scale === 1) break;
      }
    } finally {
      canvas.width = 0;
      canvas.height = 0;
    }
    throw new Error('没有识别到二维码，请选择完整清晰的截图');
  } catch (error) {
    if (error instanceof DOMException)
      throw new Error('无法读取图片，请选择 PNG 或 JPG 截图', { cause: error });
    throw error;
  } finally {
    image.src = '';
    URL.revokeObjectURL(url);
  }
}
