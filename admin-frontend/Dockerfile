FROM node:22-alpine

WORKDIR /app

RUN corepack enable && corepack prepare yarn@1.22.22 --activate

COPY admin-frontend/package.json ./
RUN yarn install --non-interactive

COPY admin-frontend ./

EXPOSE 5174

CMD ["yarn", "dev"]
