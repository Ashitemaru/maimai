import { useEffect, useRef, useState } from 'react';
import type { Chart } from './types';
import { renderCover } from './cover';
import type { CoverAspect, CoverReceipt } from './cover';
import { loadBackgroundManifest, resolveVersion } from './coverAssets';
import type { BackgroundManifest } from './coverAssets';
import { messageOf } from './api';
import { ChartTypeBadge, DifficultyBadge } from './ChartBadges';
import { ImageSaveActions } from './ImageSaveActions';
import { downloadImage, needsSaveGesture, prepareImage } from './imageSave';
import type { PreparedImage } from './imageSave';
import { useToastMessage } from './Toast';
export interface CoverState {
  chart: Chart | null;
  score: string;
  version: string;
  aspect: CoverAspect;
  safeGuides: boolean;
}
const validScore = (value: string) =>
  /^\d{1,3}(\.\d{1,4})?$/.test(value) && Number(value) >= 0 && Number(value) <= 101;
const chartName = (chart: Chart) => chart.display_title || chart.title || '（空白曲名）';
const chartType = (chart: Chart) =>
  chart.type === 'dx' ? 'DX' : chart.type === 'utage' ? 'UTAGE' : 'STD';
export function CoverPage({
  value,
  onChange,
  onPick,
  notify,
}: {
  value: CoverState;
  onChange: (value: CoverState) => void;
  onPick: () => void;
  notify: (value: string) => void;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [error, setError] = useState('');
  useToastMessage(error);
  const [exporting, setExporting] = useState(false);
  const [readyImage, setReadyImage] = useState<PreparedImage | null>(null);
  const exportGeneration = useRef(0);
  useEffect(() => {
    setReadyImage(null);
    setExporting(false);
    return () => {
      exportGeneration.current += 1;
    };
  }, [value]);
  useEffect(
    () => () => {
      if (readyImage) URL.revokeObjectURL(readyImage.url);
    },
    [readyImage],
  );
  const [rendering, setRendering] = useState(false);
  const [receipt, setReceipt] = useState<CoverReceipt | null>(null);
  const [manifest, setManifest] = useState<BackgroundManifest | null>(null);
  const change = (patch: Partial<CoverState>) => onChange({ ...value, ...patch });
  useEffect(() => {
    loadBackgroundManifest()
      .then(setManifest)
      .catch((error) => setError(messageOf(error)));
  }, []);
  useEffect(() => {
    let cancelled = false;
    const output = canvas.current;
    if (!output) return;
    if (!value.chart || !validScore(value.score)) {
      output.width = value.aspect === '4:3' ? 1440 : 1920;
      output.height = 1080;
      const ctx = output.getContext('2d')!;
      ctx.fillStyle = '#f7f3f8';
      ctx.fillRect(0, 0, output.width, 1080);
      ctx.fillStyle = '#8f8199';
      ctx.font = '36px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(value.chart ? '填写达成率以预览' : '选择谱面以预览', output.width / 2, 540);
      setError(value.score && !validScore(value.score) ? '达成率需为 0–101，最多四位小数' : '');
      setReceipt(null);
      setRendering(false);
      return;
    }
    setRendering(true);
    const temporary = document.createElement('canvas');
    renderCover(temporary, value.chart, {
      achievement: value.score,
      versionOverride: value.version,
      aspect: value.aspect,
    })
      .then((result) => {
        if (cancelled) return;
        output.width = temporary.width;
        output.height = temporary.height;
        output.getContext('2d')!.drawImage(temporary, 0, 0);
        setReceipt(result);
        setError('');
      })
      .catch((error) => {
        if (!cancelled) setError(messageOf(error));
      })
      .finally(() => {
        if (!cancelled) setRendering(false);
      });
    return () => {
      cancelled = true;
    };
  }, [value]);
  async function download() {
    if (!value.chart || !validScore(value.score)) {
      notify('请填写达成率');
      return;
    }
    if (exporting) return;
    const generation = ++exportGeneration.current;
    setReadyImage(null);
    setExporting(true);
    const output = document.createElement('canvas');
    try {
      await renderCover(output, value.chart, {
        achievement: value.score,
        versionOverride: value.version,
        aspect: value.aspect,
        safeGuides: false,
      });
      const blob = await new Promise<Blob>((resolve, reject) =>
        output.toBlob(
          (blob) => (blob ? resolve(blob) : reject(new Error('导出失败'))),
          'image/png',
        ),
      );
      const name = chartName(value.chart).replace(/[\\/:*?"<>|]/g, '_');
      const filename = `${name}_${chartType(value.chart)}_${value.chart.difficulty_name.replace(':', '')}_${Number(value.score).toFixed(4)}_${value.aspect.replace(':', 'x')}.png`;
      if (generation !== exportGeneration.current) return;
      const image = prepareImage(blob, filename);
      setReadyImage(image);
      if (needsSaveGesture()) notify('封面已生成，请选择分享或保存');
      else {
        downloadImage(image);
        notify('封面已导出');
      }
    } catch (error) {
      if (generation === exportGeneration.current) notify(messageOf(error));
    } finally {
      output.width = output.height = 0;
      if (generation === exportGeneration.current) setExporting(false);
    }
  }
  const automatic = manifest && value.chart ? resolveVersion(value.chart, manifest) : null;
  const automaticLabel = automatic?.asset
    ? `自动 · ${automatic.asset.version_name}`
    : automatic?.versionName
      ? `自动 · ${automatic.versionName}（缺少素材）`
      : '自动 · 版本待核实';
  const notices =
    receipt?.notices || (!value.version && automatic?.notice ? [automatic.notice] : []);
  useToastMessage(notices.join('\n'));
  return (
    <>
      <div className="heading">
        <div>
          <div className="eyebrow">MAKE YOUR PERFECT VISIBLE</div>
          <h1>封面工坊</h1>
        </div>
      </div>
      <div className="cover-workspace">
        <div className="preview-panel">
          <div className="preview-label">
            <span>{rendering ? '生成中…' : '实时预览'}</span>
            <span>{value.aspect === '4:3' ? '1440' : '1920'} × 1080 · PNG</span>
          </div>
          <div className="canvas-frame" aria-busy={rendering}>
            <canvas ref={canvas} width={1920} height={1080} aria-label="视频封面预览" />
          </div>
        </div>
        <aside className="cover-controls">
          <h2>封面信息</h2>
          <button type="button" className="cover-song" onClick={onPick}>
            {value.chart ? (
              <>
                <img src={value.chart.jacket_url} alt="" />
                <div className="cover-song-details">
                  <strong>{chartName(value.chart)}</strong>
                  <div className="chart-meta">
                    <ChartTypeBadge type={value.chart.type} />
                    <DifficultyBadge
                      difficulty={value.chart.difficulty}
                      name={value.chart.difficulty_name}
                      type={value.chart.type}
                    />
                  </div>
                </div>
              </>
            ) : (
              '选择谱面'
            )}
          </button>
          <div className="field">
            <label htmlFor="cover-score">达成率</label>
            <input
              id="cover-score"
              inputMode="decimal"
              value={value.score}
              onChange={(event) => change({ score: event.target.value })}
              placeholder="100.9876"
            />
          </div>
          <div className="field">
            <label htmlFor="cover-version">版本</label>
            <select
              id="cover-version"
              value={value.version}
              onChange={(event) => change({ version: event.target.value })}
            >
              <option value="">{automaticLabel}</option>
              {manifest &&
                Object.entries(manifest.version_map).map(([key, asset]) => (
                  <option key={key} value={key}>
                    {asset.version_name}
                  </option>
                ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="cover-aspect">导出比例</label>
            <select
              id="cover-aspect"
              value={value.aspect}
              onChange={(event) => change({ aspect: event.target.value as CoverAspect })}
            >
              <option value="16:9">16:9 · 1920 × 1080</option>
              <option value="4:3">4:3 · 1440 × 1080</option>
            </select>
          </div>
          <button
            className="btn primary full"
            type="button"
            disabled={exporting || rendering || !value.chart || !validScore(value.score)}
            onClick={download}
          >
            {exporting ? '导出中…' : '导出封面'}
          </button>
          {readyImage && <ImageSaveActions image={readyImage} key={readyImage.url} />}
        </aside>
      </div>
    </>
  );
}
