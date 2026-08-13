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

describe('CatalogField described-radio (webPresentation)', () => {
  const usbNode: MenuNode = {
    id: 'connectivity.usb_gadget',
    type: 'select',
    label: 'USB Gadget',
    optionSet: 'usb_gadget_mode',
    webPresentation: 'described-radio',
  };

  const usbOptions: MenuOption[] = [
    { value: 'off', label: 'Off', description: 'USB Ethernet is off.' },
    {
      value: 'auto',
      label: 'Auto',
      description: 'Board chooses Client or Shared by itself.',
    },
    {
      value: 'client',
      label: 'Client',
      description: 'Host computer shares its internet. Reach the board at http://board.local/.',
    },
    {
      value: 'shared',
      label: 'Shared',
      description: 'Board runs the USB network at http://10.12.194.1/.',
    },
  ];

  it('renders a radio per option with every description visible and writes the value', () => {
    // Why: USB Gadget must show Off/Auto/Client/Shared as radios with
    // always-visible blurbs, not a dropdown that hides unselected modes -- the
    // descriptions are how the modes are told apart. How a regression manifests:
    // a combobox appears, a description is missing, or onChange does not receive
    // the option value.
    const onChange = vi.fn();
    render(
      <CatalogField
        node={usbNode}
        value="client"
        options={usbOptions}
        onChange={onChange}
      />,
    );

    const radios = screen.getAllByRole('radio') as HTMLInputElement[];
    expect(radios.map((radio) => radio.value)).toEqual(['off', 'auto', 'client', 'shared']);
    expect((radios.find((r) => r.value === 'client') as HTMLInputElement).checked).toBe(true);
    expect(screen.queryByRole('combobox')).toBeNull();
    // Stacked full-width layout so long descriptions are not squeezed into the
    // FormRow's right column (which truncated Shared mid-sentence on desktop).
    expect(document.querySelector('.form-row--stacked')).not.toBeNull();
    expect(screen.getByText('USB Ethernet is off.')).toBeInTheDocument();
    expect(screen.getByText('Board chooses Client or Shared by itself.')).toBeInTheDocument();
    expect(
      screen.getByText('Host computer shares its internet. Reach the board at http://board.local/.'),
    ).toBeInTheDocument();
    expect(
      screen.getByText('Board runs the USB network at http://10.12.194.1/.'),
    ).toBeInTheDocument();

    fireEvent.click(radios.find((r) => r.value === 'shared') as HTMLInputElement);
    expect(onChange).toHaveBeenCalledWith('shared');
  });

  it('uses a caller-supplied label in place of the node label, including the group name', () => {
    // Why: the USB Gadget card takes its title from this same node, so rendering
    // node.label here printed "USB Gadget" twice, once as the card heading and
    // again as the field above the radios. The card passes a distinct label for
    // the control instead. The accessible name of the radiogroup has to follow
    // it, or a screen reader announces a group whose name is not on screen.
    // How a regression manifests: the override is ignored, the heading text is
    // duplicated, and the group is named after the card rather than the control.
    render(
      <CatalogField
        node={usbNode}
        label="Gadget Mode"
        value="client"
        options={usbOptions}
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByText('Gadget Mode')).toBeInTheDocument();
    expect(screen.queryByText('USB Gadget')).toBeNull();
    expect(screen.getByRole('radiogroup', { name: 'Gadget Mode' })).toBeInTheDocument();
  });

  it('falls back to the node label when the caller supplies none', () => {
    // Why: the override is for the one card that also shows this node as its
    // title; every other caller must keep getting the catalog's label, so the
    // menu stays data-driven. How a regression manifests: fields elsewhere lose
    // their labels, or start showing a node id.
    render(
      <CatalogField node={usbNode} value="off" options={usbOptions} onChange={vi.fn()} />,
    );

    expect(screen.getByText('USB Gadget')).toBeInTheDocument();
    expect(screen.getByRole('radiogroup', { name: 'USB Gadget' })).toBeInTheDocument();
  });

  it('keeps a dropdown when every option has a description but webPresentation is unset', () => {
    // Why: time-control presets also carry descriptions and must stay a
    // dropdown. Inferring radios from description alone would turn that long
    // list into a radio wall. How a regression manifests: radios render without
    // webPresentation.
    const onChange = vi.fn();
    render(
      <CatalogField
        node={{ id: 'game.time_control_preset', type: 'select', label: 'Preset' }}
        value="blitz"
        options={[
          { value: 'blitz', label: '5|3 Blitz', description: '5 minutes plus 3 seconds.' },
          { value: 'rapid', label: '10|5 Rapid', description: '10 minutes plus 5 seconds.' },
        ]}
        onChange={onChange}
      />,
    );
    expect(screen.getByRole('combobox')).toBeInTheDocument();
    expect(screen.queryByRole('radio')).toBeNull();
  });
});
