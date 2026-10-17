const SIZES = {
  sm: { mark: 20, fontSize: 'text-sm', gap: 'gap-1.5' },
  md: { mark: 28, fontSize: 'text-base', gap: 'gap-2' },
  lg: { mark: 36, fontSize: 'text-xl', gap: 'gap-2.5' },
}

const HIVE_CELLS = [
  { id: 'upper-left', cx: 9.35, cy: 10.05, tone: 'sun' },
  { id: 'upper-right', cx: 22.65, cy: 10.05, tone: 'gold' },
  { id: 'lower', cx: 16, cy: 21.65, tone: 'sun' },
]

function hexPath(cx, cy, radius = 4.45) {
  const half = radius / 2
  const rise = (Math.sqrt(3) * radius) / 2
  return [
    `M ${cx + radius} ${cy}`,
    `L ${cx + half} ${cy + rise}`,
    `L ${cx - half} ${cy + rise}`,
    `L ${cx - radius} ${cy}`,
    `L ${cx - half} ${cy - rise}`,
    `L ${cx + half} ${cy - rise}`,
    'Z',
  ].join(' ')
}

/** Three substantial cells form a compact manufactured hive, not a radial flower. */
export function HoneycombMark({ size = 28 }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className="hive-cell-hive-mark shrink-0"
      aria-hidden="true"
      data-hive-mark="cell-hive"
    >
      {HIVE_CELLS.map((cell) => (
        <path
          key={cell.id}
          className={`hive-cell-hive-cell hive-cell-hive-cell-${cell.tone}`}
          data-hive-cell={cell.id}
          d={hexPath(cell.cx, cell.cy, 6.25)}
        />
      ))}
    </svg>
  )
}

export function HiveWordmark({
  size = 'md',
  markOnly = false,
  textOnly = false,
  showSubtitle = false,
  className = '',
}) {
  const dimensions = SIZES[size] || SIZES.md
  return (
    <div className={`flex items-center ${dimensions.gap} ${className}`}>
      {!textOnly && <HoneycombMark size={dimensions.mark} />}
      {!markOnly && (
        <div className="flex flex-col leading-none">
          <span
            className={`font-semibold text-amber-500 dark:text-amber-400 ${dimensions.fontSize}`}
            style={{ letterSpacing: '-0.02em' }}
          >
            hive
          </span>
          {showSubtitle && (
            <span className="mt-0.5 text-[10px] text-gray-500 dark:text-gray-400">
              Agent Home
            </span>
          )}
        </div>
      )}
    </div>
  )
}

export default HiveWordmark
