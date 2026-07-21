// In-process synthetic NRRD volume generator: a minimal valid NRRD (ASCII
// header + blank line + raw little-endian int16 voxels) that itk-wasm loads and
// renders. The voxels are a gradient so the render is visibly non-empty, with
// two `variant`s so the uploaded images look distinct.
export type NrrdOptions = {
  size?: number; // cube edge length in voxels (default 16)
  variant?: number; // 0 or 1 — flips the gradient so a.nrrd != b.nrrd visually
};

export function makeNrrd({ size = 16, variant = 0 }: NrrdOptions = {}): Buffer {
  // Header — one field per line, terminated by a single blank line. Keep the
  // geometry keys itk-wasm's NrrdImageIO expects for a well-formed LPS volume.
  const header =
    [
      'NRRD0004',
      '# Synthetic e2e test volume (generated in-process)',
      'type: short',
      'dimension: 3',
      `sizes: ${size} ${size} ${size}`,
      'kinds: domain domain domain',
      'encoding: raw',
      'endian: little',
      'space: left-posterior-superior',
      'space directions: (1,0,0) (0,1,0) (0,0,1)',
      'space origin: (0,0,0)',
    ].join('\n') + '\n\n'; // trailing blank line separates header from data

  const headerBuf = Buffer.from(header, 'ascii');
  const data = Buffer.alloc(size * size * size * 2); // int16 = 2 bytes/voxel

  // NRRD raw order: the first-listed axis varies fastest, so x is innermost.
  const span = 3 * (size - 1) || 1;
  let offset = 0;
  for (let z = 0; z < size; z++) {
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        const g = (x + y + z) / span; // 0..1 corner-to-corner gradient
        const norm = variant ? 1 - g : g;
        const value = Math.round(norm * 3000); // fits comfortably in int16
        data.writeInt16LE(value, offset);
        offset += 2;
      }
    }
  }

  return Buffer.concat([headerBuf, data]);
}
