
import path from 'node:path';
import { pathToFileURL } from 'node:url';

// returns true if the passsed file is being invoked from node,
// not imported. `node -e` / stdin runs have no argv[1]: nothing is main.
/** @param {string} importMetaUrl @returns {boolean} */
import fs from 'node:fs';

export function isMain(importMetaUrl) {
    const entry = process.argv[1];
    if (entry === undefined) return false;
    const resolved = path.resolve(entry);
    if (importMetaUrl === pathToFileURL(resolved).href) return true;
    try {
        return importMetaUrl === pathToFileURL(fs.realpathSync(resolved)).href;
    } catch {
        return false;
    }
}
