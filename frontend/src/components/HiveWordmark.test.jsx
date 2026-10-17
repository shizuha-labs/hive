import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { HiveWordmark } from './HiveWordmark'

describe('HiveWordmark', () => {
  it('renders a compact three-cell hive instead of a radial flower cluster', () => {
    const html = renderToStaticMarkup(<HiveWordmark size="md" showSubtitle />)

    expect(html).toContain('data-hive-mark="cell-hive"')
    expect(html.match(/data-hive-cell=/g)).toHaveLength(3)
    expect(html).not.toContain('hive-cell-hive-slot')
    expect(html).toContain('data-hive-cell="lower"')
    expect(html).not.toContain('hive-skep')
    expect(html).toContain('Agent Home')
  })
})
