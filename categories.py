"""Известные slug'и Авито и словарь городов. LLM выбирает slug из списка
(или может предложить свой — тогда бот проверит страницу и при нуле
результатов откатится на поиск без категории: ?q=...)."""

CATEGORIES = {
    "telefony": "Телефоны (смартфоны, кнопочные)",
    "noutbuki": "Ноутбуки",
    "planshety": "Планшеты и электронные книги",
    "monitory": "Мониторы",
    "komputery": "Компьютеры (сборки, стационарные)",
    "komplektuyuschie": "Комплектующие (видеокарты, процессоры, память)",
    "igrovye_konsoli": "Игровые приставки",
    "televisions": "Телевизоры и проекционная техника",
    "audio_i_video": "Аудио и видеотехника (колонки, наушники, плееры)",
    "bytovaya_tehnika": "Бытовая техника (холодильники, стиральные машины)",
    "fot_video_tehnika": "Фото и видеотехника (камеры, объективы)",
    "gadgety_i_nositimye": "Гаджеты и носимые устройства (смарт-часы, браслеты)",
    "mebel": "Мебель и интерьер",
    "pokolnoe": "Посуда и товары для кухни",
    "velosipedy": "Велосипеды",
    "zapchasti": "Запчасти для автомобилей",
    "shiny_i_diski": "Шины, диски и колёса",
    "avtomobili": "Автомобили",
    "motocikly": "Мотоциклы и мототехника",
    "detskie_tovary": "Детские товары и игрушки",
    "odezhda": "Одежда, обувь и аксессуары",
    "instrumenty": "Инструменты",
    "krasota_i_zdorovye": "Красота и здоровье",
    "sport_i_otdyh": "Спорт и отдых",
    "hobby_i_otdyh": "Хобби и отдых",
    "zhivotnye": "Животные",
}

# известные города: ключи — варианты написания, значения — слаги Авито
CITIES = {
    "россия": "rossiya",
    "вся россия": "rossiya",
    "москва": "moskva",
    "мск": "moskva",
    "санкт-петербург": "spb",
    "спб": "spb",
    "питер": "spb",
    "новосибирск": "novosibirsk",
    "кемерово": "kemerovo",
    "новокузнецк": "novokuznetsk",
    "екатеринбург": "ekaterinburg",
    "красноярск": "krasnoyarsk",
    "омск": "omsk",
    "казань": "kazan",
    "самара": "samara",
    "воронеж": "voronezh",
    "ростов-на-дону": "rostov-na-donu",
    "краснодар": "krasnodar",
}


def categories_prompt_line() -> str:
    return "\n".join(f"  - {slug} — {name}" for slug, name in CATEGORIES.items())


def cities_prompt_line() -> str:
    return "\n".join(f"  - {slug} — {city}" for city, slug in CITIES.items())
