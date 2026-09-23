/// <reference types="vite/client" />

// Repository README bundled at build time by the `bundle-readme` Vite plugin.
// The About page renders its Acknowledgments section from this single source.
declare module 'virtual:readme' {
  const content: string;
  export default content;
}

// Content hashes of packaged images, keyed by the URL path the browser
// requests. Produced by the version-static-images Vite plugin.
declare module 'virtual:static-image-versions' {
  export const staticImageVersions: Readonly<Record<string, string>>;
}
