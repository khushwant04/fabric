# Hexel Studio components

Copied from the Hexel Studio ui-registry (`registry/hexelstudio/ui/`, commit
462f129), the way `npx shadcn add @hexelstudio/<name>` installs
them: the source is committed here, so building the console never needs the registry.
Only the import paths were changed. To take an update, copy the file again and review
the diff; the theme they read comes from `design-system/` (see `scripts/build-hexel-css.mjs`).

| File | Registry item |
|---|---|
| `panel.tsx` | `@hexelstudio/panel` |
| `stat-tile.tsx` | `@hexelstudio/stat-tile` |
| `sparkline.tsx` | `@hexelstudio/sparkline` |
| `status.tsx` | `@hexelstudio/status` |
| `meter.tsx` | `@hexelstudio/meter` |
| `bar-list.tsx` | `@hexelstudio/bar-list` |
| `table.tsx` | `@hexelstudio/table` |
