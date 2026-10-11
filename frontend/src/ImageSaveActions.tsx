import { useState } from 'react';
import type { PreparedImage } from './imageSave';

export function ImageSaveActions({ image }: { image: PreparedImage }) {
  const [sharing, setSharing] = useState(false);
  const [error, setError] = useState('');
  const canShare = !!navigator.canShare?.({ files: [image.file] });
  async function share() {
    if (sharing) return;
    setSharing(true);
    setError('');
    try {
      // No asynchronous preparation precedes this call: it keeps the tap's user activation.
      await navigator.share({ files: [image.file], title: image.file.name });
    } catch (error) {
      if (!(error instanceof DOMException && error.name === 'AbortError')) {
        setError('分享未完成，可选择保存 PNG，或打开整张图片后长按保存。');
      }
    } finally {
      setSharing(false);
    }
  }
  return (
    <section className="image-save-panel" aria-label="图片已准备好">
      <p role="status">整张 PNG 已生成，可以分享或保存。</p>
      <div className="image-save-actions">
        {canShare && (
          <button type="button" className="btn" onClick={share} disabled={sharing}>
            {sharing ? '分享中…' : '分享图片'}
          </button>
        )}
        <a className="btn" href={image.url} download={image.file.name}>
          保存 PNG
        </a>
        <a className="btn" href={image.url} target="_blank" rel="noopener noreferrer">
          打开整张图片
        </a>
      </div>
      <p className="image-save-help">iPhone 可在分享菜单中存储图像；也可打开图片后长按保存。</p>
      {error && (
        <div className="form-message" role="alert">
          {error}
        </div>
      )}
    </section>
  );
}
