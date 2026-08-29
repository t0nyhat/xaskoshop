# ХаскоШоп — статический MVP

Главная страница: `index.html`. Её можно открыть двойным кликом — сервер и интернет для загрузки интерфейса не нужны.

Опубликованная версия: <https://t0nyhat.github.io/xaskoshop/>

Все рабочие ресурсы находятся локально:

- `assets/css/styles.css` — собранные стили;
- `assets/fonts/` — локальные шрифты;
- `assets/images/lifestyle/` — фотографии разделов;
- `assets/images/products/` — фотографии упаковок.
- `assets/images/brand/logo-mark.jpg` — компактный фирменный знак для шапки и favicon;
- `assets/images/brand/logo-full.jpg` — полный фирменный логотип в контактах и для превью ссылки.

Контакты, адрес, ссылки на мессенджеры и режим отображения цен меняются в объекте `CONFIG` в начале JavaScript внутри HTML.

Если меняются Tailwind-классы, CSS пересобирается командой:

```sh
npm_config_cache=/tmp/xaskoshop-npm-cache npx --yes tailwindcss@3.4.17 -c tailwind.config.cjs -i assets/css/input.css -o assets/css/styles.css --minify
```

Источники изображений и важная правовая оговорка указаны в `assets/IMAGE_SOURCES.md`.
