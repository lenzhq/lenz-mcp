import { test } from 'node:test';
import assert from 'node:assert/strict';

import { isFullBleed } from './bleed.js';

const touch = { matchMedia: (q) => ({ matches: q === '(pointer: coarse)' }) };
const mouse = { matchMedia: () => ({ matches: false }) };
const phone = (innerWidth, width, height) => ({ ...touch, innerWidth, screen: { width, height } });

test('a frame as wide as an upright phone is full-bleed', () => {
  assert.equal(isFullBleed(phone(390, 390, 844)), true);
  assert.equal(isFullBleed(phone(360, 360, 800)), true);
});

test('an inset frame is not', () => {
  assert.equal(isFullBleed(phone(358, 390, 844)), false);
  assert.equal(isFullBleed(phone(700, 390, 844)), false, 'inset in landscape, wider than the portrait width');
});

test('landscape: iOS keeps screen.width upright, Android swaps it; both read as full-bleed', () => {
  assert.equal(isFullBleed(phone(844, 390, 844)), true);
  assert.equal(isFullBleed(phone(800, 800, 360)), true);
});

test('a desktop column is never full-bleed, even one as wide as the screen is tall', () => {
  assert.equal(isFullBleed({ ...mouse, innerWidth: 768, screen: { width: 1366, height: 768 } }), false);
  assert.equal(isFullBleed({ innerWidth: 390, screen: { width: 390, height: 844 } }), false, 'no matchMedia');
});

test('missing numbers never flag full-bleed', () => {
  assert.equal(isFullBleed(undefined), false);
  assert.equal(isFullBleed(phone(0, 390, 844)), false);
  assert.equal(isFullBleed({ ...touch, innerWidth: 390 }), false);
  assert.equal(isFullBleed(phone(390, 0, 0)), false);
});

test('one pixel of rounding slack', () => {
  assert.equal(isFullBleed(phone(389, 390, 844)), true);
  assert.equal(isFullBleed(phone(388, 390, 844)), false);
});
