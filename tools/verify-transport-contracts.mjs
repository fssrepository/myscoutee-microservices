#!/usr/bin/env node
// Reuse the backend checkout's existing frontend Ajv; no new tool installation.
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const backend = path.resolve(process.env.MYSCOUTEE_BACKEND_SOURCE || process.cwd());
const root = path.join(backend, 'config/microservices');
const build = path.join(backend, 'server/build/microservices');
const require = createRequire(path.join(backend, 'frontend/package.json'));
const Ajv2020 = require('ajv/dist/2020');
const addFormats = require('ajv-formats');
const schemasOnly = process.argv.includes('--schemas-only');
const selected = process.argv.slice(2).filter(value => value.startsWith('--contract='));
if (selected.length > 1 || process.argv.slice(2).some(value => value !== '--schemas-only' && !value.startsWith('--contract=')))
  throw new Error('Usage: verify-transport-contracts.mjs [--schemas-only] [--contract=domain/name-v1]');
const selectedContract = selected.length ? selected[0].slice('--contract='.length) : null;
let matched = false;
for (const [contract, subdirectory, requiredStatuses] of JSON.parse(fs.readFileSync(path.join(root, 'transport/contracts/evidence-index.json'), 'utf8'))) {
  if (selectedContract !== null && contract !== selectedContract) continue;
  matched = true;
  const spec = JSON.parse(fs.readFileSync(path.join(root, `transport/contracts/${contract}.openapi.json`), 'utf8'));
  const validator = new Ajv2020();
  addFormats(validator);
  const rewriteReferences = value => JSON.parse(JSON.stringify(value, (key, item) =>
    key === '$ref' && typeof item === 'string' && item.startsWith('#/components/schemas/')
      ? item.replace('#/components/schemas/', '#/$defs/') : item));
  const definitions = rewriteReferences(spec.components.schemas);
  const compile = schema => validator.compile({ $defs: definitions, ...rewriteReferences(schema) });
  const responseValidators = new Map();
  for (const [route, operations] of Object.entries(spec.paths)) {
    for (const [method, operation] of Object.entries(operations)) {
      for (const media of Object.values(operation.requestBody?.content || {})) compile(media.schema);
      for (const [status, response] of Object.entries(operation.responses || {})) {
        for (const [mediaType, media] of Object.entries(response.content || {})) {
          responseValidators.set(`${route} ${method} ${status} ${mediaType}`, compile(media.schema));
        }
      }
    }
  }
  if (schemasOnly) {
    console.log(`Compiled request/response schemas for ${contract}; HTTP behavior not verified.`);
    continue;
  }
  const directory = path.join(build, 'bootstrap-tests/wire-evidence', subdirectory);
  const files = fs.existsSync(directory) ? fs.readdirSync(directory).filter(name => /^response-[0-9]+\.json$/.test(name)) : [];
  if (!files.length) throw new Error(`Run :bootstrap-tests:test first; no real HTTP evidence exists for ${contract}.`);
  const statuses = new Set();
  for (const file of files) {
    const result = JSON.parse(fs.readFileSync(path.join(directory, file), 'utf8'));
    const response = spec.paths[result.path][result.method || 'get'].responses[String(result.status)];
    const stream = response.content['application/x-ndjson'];
    const validate = responseValidators.get(`${result.path} ${result.method || 'get'} ${result.status} ${stream ? 'application/x-ndjson' : 'application/json'}`);
    const bodies = stream ? result.frames : [result.body];
    if (!Array.isArray(bodies) || !bodies.length) throw new Error(`${contract}/${file}: missing response frames`);
    for (const body of bodies) {
      if (!validate(body)) throw new Error(`${contract}/${file}: ${JSON.stringify(validate.errors)}`);
    }
    if (stream) {
      const terminal = bodies.at(-1);
      const measurement = spec['x-stream-protocol'] === 'measurement-evidence-v1';
      if (measurement) {
        if (result.complete !== true || terminal?.kind !== 'complete' || terminal.count !== bodies.length - 1
            || bodies.slice(0, -1).some(frame => frame.kind !== 'evidence')) {
          throw new Error(`${contract}/${file}: incomplete or malformed measurement stream`);
        }
      } else if (result.complete !== true || !['result', 'error'].includes(terminal?.kind)
          || bodies.slice(0, -1).some(frame => frame.kind !== 'progress')) {
        throw new Error(`${contract}/${file}: incomplete or malformed job stream`);
      }
    }
    statuses.add(result.status);
  }
  for (const status of requiredStatuses) {
    if (!statuses.has(status)) throw new Error(`${contract}: missing HTTP status evidence: ${status}`);
  }
  console.log(`Validated ${files.length} real HTTP responses against ${contract}.openapi.json; statuses: ${[...statuses].sort((left, right) => left - right).join(', ')}.`);
}
if (!matched) throw new Error(`Unknown contract: ${selectedContract}`);
