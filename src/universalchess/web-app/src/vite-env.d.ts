/// <reference types="vite/client" />

// Repository README bundled at build time by the `bundle-readme` Vite plugin.
// The About page renders its Acknowledgments section from this single source.
declare module 'virtual:readme' {
  const content: string;
  export default content;
}
