import { defineConfig } from "vite";

export default defineConfig({
  root: "trac/static",
  plugins: [
    {
      name: "trac-dashboard-root",
      configureServer(server) {
        const staticAssets = new Set([
          "apple-touch-icon.png",
          "dashboard.css",
          "favicon-48x48.png",
          "favicon-180x180.png",
          "favicon-512x512.png",
          "favicon.svg",
          "trac-logo.svg",
          "trac-logo-light.svg",
          "trac-symbol.svg",
          "trac-symbol-light.svg",
        ]);
        server.middlewares.use((request, response, next) => {
          const pathname = request.url.split("?")[0];
          if (pathname.startsWith("/static/") && staticAssets.has(pathname.slice("/static/".length))) {
            request.url = request.url.replace("/static/", "/");
          }
          if (request.url === "/") {
            response.statusCode = 302;
            response.setHeader("Location", "/dashboard.html");
            response.end();
            return;
          }
          next();
        });
      },
    },
  ],
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/v1": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
