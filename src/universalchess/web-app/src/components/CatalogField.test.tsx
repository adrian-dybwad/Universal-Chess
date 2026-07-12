// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { CatalogField } from './CatalogField';
import type { MenuNode, MenuOption } from '../types/menuCatalog';

/**
 * Tests for CatalogField's option-list presentations.
 *
 * Why these exist: the Display tab's Sprites (image) and Text Size (scaled
 * preview) pickers were bespoke React; they now render declaratively from the
 * option data (an option's `image` -> image radio grid; `font_size` -> scaled
 * text preview) so the whole tab comes from the catalog. These pin that the
 * presentation is chosen from the options alone and that selection writes the
 * option value -- a regression would fall back to a plain dropdown (losing the
 * visual pick) or write the wrong value.
 */

afterEach(cleanup);

const spriteNode: MenuNode = {
  id: 'field.display.sprites',
  type: 'dynamic',
  label: 'Piece Sprites',
  itemBind: { store: 'game', key: 'chess_sprites' },
};

const textSizeNode: MenuNode = {
  id: 'field.display.text_size',
  type: 'select',
  label: 'Text Size',
  optionSet: 'text_size',
};

const spriteOptions: MenuOption[] = [
  { value: 'default', label: 'Default', image: '/api/sprites/default/image' },
  { value: 'cburnett', label: 'Cburnett', image: '/api/sprites/cburnett/image' },
];

const textSizeOptions: MenuOption[] = [
  { value: 'small', label: 'Small', font_size: 13 },
  { value: 'medium', label: 'Medium', font_size: 16 },
  { value: 'large', label: 'Large', font_size: 20 },
];

describe('CatalogField image radio (options with image)', () => {
  it('renders an image radio per option and writes the chosen value', () => {
    // Every option carries an image, so this must be an image grid (radios +
    // <img>), not a dropdown. Regression: a dropdown renders (no images) or the
    // onChange payload is not the option value.
    const onChange = vi.fn();
    render(
      <CatalogField
        node={spriteNode}
        value="default"
        options={spriteOptions}
        onChange={onChange}
      />,
    );

    const radios = screen.getAllByRole('radio') as HTMLInputElement[];
    expect(radios).toHaveLength(2);
    // The selected value is reflected, and each option shows its preview image.
    expect((radios.find((r) => r.value === 'default') as HTMLInputElement).checked).toBe(true);
    const images = screen.getAllByRole('img');
    expect(images.map((i) => i.getAttribute('src'))).toEqual([
      '/api/sprites/default/image',
      '/api/sprites/cburnett/image',
    ]);
    // No dropdown fallback rendered.
    expect(screen.queryByRole('combobox')).toBeNull();

    fireEvent.click(radios.find((r) => r.value === 'cburnett') as HTMLInputElement);
    expect(onChange).toHaveBeenCalledWith('cburnett');
  });
});

describe('CatalogField text-size preview (options with font_size)', () => {
  it('renders a scaled sample per option at the option font size', () => {
    // Every option carries a font_size, so each renders a sample line sized to
    // that value -- the by-eye pick. Regression: a plain dropdown renders, or the
    // sample is not sized from font_size (all previews identical).
    const onChange = vi.fn();
    render(
      <CatalogField
        node={textSizeNode}
        value="medium"
        options={textSizeOptions}
        onChange={onChange}
      />,
    );

    const radios = screen.getAllByRole('radio') as HTMLInputElement[];
    expect(radios).toHaveLength(3);
    expect((radios.find((r) => r.value === 'medium') as HTMLInputElement).checked).toBe(true);
    expect(screen.queryByRole('combobox')).toBeNull();

    // The three sample lines are sized to 13/16/20px respectively.
    const samples = document.querySelectorAll('.text-size-option-sample');
    expect(Array.from(samples).map((s) => (s as HTMLElement).style.fontSize)).toEqual([
      '13px',
      '16px',
      '20px',
    ]);

    fireEvent.click(radios.find((r) => r.value === 'large') as HTMLInputElement);
    expect(onChange).toHaveBeenCalledWith('large');
  });
});

describe('CatalogField dropdown fallback (plain options)', () => {
  it('renders a dropdown when options carry neither image nor font_size', () => {
    // The guard that the rich presentations are opt-in via data: ordinary
    // options must still render the standard <select>. Regression: a plain
    // option list wrongly renders as an (empty) image/preview grid.
    const onChange = vi.fn();
    render(
      <CatalogField
        node={{ id: 'x', type: 'select', label: 'Plain' }}
        value="a"
        options={[
          { value: 'a', label: 'A' },
          { value: 'b', label: 'B' },
        ]}
        onChange={onChange}
      />,
    );
    expect(screen.getByRole('combobox')).toBeInTheDocument();
    expect(screen.queryByRole('radio')).toBeNull();
  });
});
