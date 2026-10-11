export interface PreparedImage {
  file: File;
  url: string;
}

export function prepareImage(blob: Blob, name: string): PreparedImage {
  return { file: new File([blob], name, { type: 'image/png' }), url: URL.createObjectURL(blob) };
}

export function needsSaveGesture() {
  return (
    /iPhone|iPad|iPod/.test(navigator.userAgent) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)
  );
}

export function downloadImage(image: PreparedImage) {
  const link = document.createElement('a');
  link.href = image.url;
  link.download = image.file.name;
  document.body.append(link);
  link.click();
  link.remove();
}
