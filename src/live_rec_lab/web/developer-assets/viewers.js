"use strict";

const swagger = document.getElementById("swagger-ui");
if (swagger) {
  const token = document.querySelector('meta[name="csrf-token"]').content;
  SwaggerUIBundle({
    domNode: swagger,
    url: "/openapi.json",
    validatorUrl: null,
    persistAuthorization: false,
    requestInterceptor(request) {
      const endpoint = new URL(request.url, location.origin);
      if (endpoint.origin !== location.origin) throw new Error("Local API only");
      if (endpoint.pathname.startsWith("/api/v1/")) request.headers["X-CSRF-Token"] = token;
      return request;
    }
  });
}

const redoc = document.getElementById("redoc");
if (redoc) {
  Redoc.init("/openapi.json", {disableGoogleFont: true, disableTelemetry: true}, redoc);
}
