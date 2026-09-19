import { buildStates as build } from '../src/dev/states.js';
import { FIXTURES } from './harness.mjs';

export const buildStates = () => build(FIXTURES);
