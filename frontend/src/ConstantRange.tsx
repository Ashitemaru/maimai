import { useRef, useState } from 'react';
import type { KeyboardEvent, PointerEvent } from 'react';

type Interval = [number, number];
type Bound = 0 | 1;

export function ConstantRange({
  min,
  max,
  value,
  onChange,
}: {
  min: number;
  max: number;
  value: Interval;
  onChange: (value: Interval) => void;
}) {
  const rail = useRef<HTMLDivElement>(null);
  const handles = useRef<(SVGGElement | null)[]>([]);
  const drag = useRef<{ pointerId: number; bound: Bound | null } | null>(null);
  const [draft, setDraft] = useState<Interval | null>(null);
  const [activeBound, setActiveBound] = useState<Bound | null>(null);
  const shown = draft ?? value;
  const latest = useRef(value);
  if (!drag.current) latest.current = value;
  const span = max - min || 1;
  const percent = (number: number) => ((number - min) / span) * 100;
  const snap = (number: number) => Math.round(Math.min(max, Math.max(min, number)) * 10) / 10;

  function update(bound: Bound, number: number) {
    const next: Interval = [...latest.current];
    next[bound] = bound === 0 ? Math.min(snap(number), next[1]) : Math.max(snap(number), next[0]);
    if (next[0] === latest.current[0] && next[1] === latest.current[1]) return;
    latest.current = next;
    if (drag.current) setDraft(next);
    else onChange(next);
  }

  function pointerValue(event: PointerEvent<HTMLDivElement>) {
    const rect = rail.current!.getBoundingClientRect();
    return snap(min + ((event.clientX - rect.left) / rect.width) * span);
  }

  function move(event: PointerEvent<HTMLDivElement>) {
    if (drag.current?.pointerId !== event.pointerId) return;
    const number = pointerValue(event);
    if (drag.current.bound === null) {
      if (number === latest.current[0]) return;
      drag.current.bound = number < latest.current[0] ? 0 : 1;
      handles.current[drag.current.bound]?.focus({ preventScroll: true });
      setActiveBound(drag.current.bound);
    }
    update(drag.current.bound, number);
  }

  function start(event: PointerEvent<HTMLDivElement>) {
    if (!event.isPrimary || event.button !== 0) return;
    event.preventDefault();
    const number = pointerValue(event);
    const [lower, upper] = latest.current;
    const bound =
      lower === upper && number === lower
        ? null
        : Math.abs(number - lower) < Math.abs(number - upper)
          ? 0
          : 1;
    drag.current = { pointerId: event.pointerId, bound };
    event.currentTarget.setPointerCapture(event.pointerId);
    if (bound !== null) {
      handles.current[bound]?.focus({ preventScroll: true });
      update(bound, number);
    }
    setActiveBound(bound);
  }

  function keyDown(event: KeyboardEvent<SVGGElement>, bound: Bound) {
    const deltas: Record<string, number> = {
      ArrowLeft: -0.1,
      ArrowDown: -0.1,
      ArrowRight: 0.1,
      ArrowUp: 0.1,
      PageDown: -1,
      PageUp: 1,
    };
    if (event.key === 'Home') update(bound, bound === 0 ? min : latest.current[0]);
    else if (event.key === 'End') update(bound, bound === 0 ? latest.current[1] : max);
    else if (event.key in deltas) update(bound, latest.current[bound] + deltas[event.key]);
    else return;
    setActiveBound(bound);
    event.preventDefault();
  }

  function finish(event: PointerEvent<HTMLDivElement>) {
    if (drag.current?.pointerId !== event.pointerId) return;
    move(event);
    onChange(latest.current);
    drag.current = null;
    setDraft(null);
    setActiveBound(null);
  }

  function cancel() {
    drag.current = null;
    setDraft(null);
    setActiveBound(null);
  }

  return (
    <div className="constant-range" role="group" aria-label="定数区间">
      <div className="range-heading">
        <span>定数</span>
        <output>
          {shown[0].toFixed(1)} – {shown[1].toFixed(1)}
        </output>
      </div>
      <div
        className="range-control"
        onPointerDown={start}
        onPointerMove={move}
        onPointerUp={finish}
        onPointerCancel={cancel}
        onLostPointerCapture={cancel}
      >
        <div className="range-rail" ref={rail}>
          <svg className="range-graphic" width="100%" height="48">
            <rect className="range-track" x="0" y="21" width="100%" height="6" rx="3" />
            <rect
              className="range-selection"
              x={`${percent(shown[0])}%`}
              y="21"
              width={`${percent(shown[1]) - percent(shown[0])}%`}
              height="6"
              rx="3"
            />
            {([0, 1] as const).map((bound) => (
              <svg
                key={bound}
                className="range-marker"
                x={`${percent(shown[bound])}%`}
                y="0"
                width="1"
                height="48"
              >
                <g
                  role="slider"
                  tabIndex={0}
                  className={'range-handle' + (activeBound === bound ? ' active' : '')}
                  ref={(element) => {
                    handles.current[bound] = element;
                  }}
                  aria-label={bound === 0 ? '最低定数' : '最高定数'}
                  aria-orientation="horizontal"
                  aria-valuemin={bound === 0 ? min : shown[0]}
                  aria-valuemax={bound === 0 ? shown[1] : max}
                  aria-valuenow={shown[bound]}
                  aria-valuetext={shown[bound].toFixed(1)}
                  onKeyDown={(event) => keyDown(event, bound)}
                  onBlur={() => setActiveBound(null)}
                >
                  <circle className="range-hit" cx="0" cy="24" r="20" />
                  <circle className="range-knob" cx="0" cy="24" r="8" />
                  {activeBound === bound && (
                    <g className="range-tooltip" aria-hidden="true">
                      <rect x="-24" y="-17" width="48" height="28" rx="6" />
                      <text x="0" y="-3" dy=".35em">
                        {shown[bound].toFixed(1)}
                      </text>
                    </g>
                  )}
                </g>
              </svg>
            ))}
          </svg>
        </div>
      </div>
    </div>
  );
}
