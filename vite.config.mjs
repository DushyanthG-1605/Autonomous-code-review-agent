import { defineConfig } from "vite";

export default defineConfig({
  root: "trac/static",
  plugins: [
    {
      name: "trac-dashboard-root",
      configureServer(server) {
        server.middlewares.use((request, response, next) => {
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
    },
  },
});
