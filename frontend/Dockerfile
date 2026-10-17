# Development Dockerfile for the Shizuha HIVE frontend (vite dev server).
# In docker-compose dev, `../../packages` is volume-mounted to /packages and the
# frontend dir to /app; vite/tailwind prefer /packages/shizuha-ui/src when present.
FROM node:20-alpine

WORKDIR /app

# Created for the dev volume mount of the shared packages.
RUN mkdir -p /packages

# Install deps. The file:../../packages/shizuha-ui dep can't resolve at image
# build time (packages arrive via volume mount at runtime), so don't fail on it.
COPY package*.json ./
COPY vendor/ ./vendor/
RUN npm ci --ignore-scripts --no-audit --no-fund

COPY . .

EXPOSE 80

CMD ["npm", "run", "dev"]
