/**
 * web/js/ui.js
 * Comprehensive UI Components and Views Renderer for Logovo.bet (v2.0).
 */

import { store } from './store.js';
import { tgBridge } from './tg.js';

// Все 80 клубов сезона, порядок как в config.DIVISION_CLUBS. Это независимая копия
// TEAM_LOGO_MAP из services/graphics/table_generator.py: Mini App отдаёт логотипы сам,
// мимо Pillow. Обе карты надо править вместе — имена файлов обязаны совпадать.
export const TEAM_LOGO_MAP = {
  // DIV_1
  'лидс': 'leeds.png',
  'ренн': 'rennes.png',
  'ницца': 'nice.png',
  'нэшвилл': 'nashville.png',
  'порту': 'porto.png',
  'вест хэм': 'west_ham.png',
  'вольфсбург': 'wolfsburg.png',
  'фиорентина': 'fiorentina.png',
  'лацио': 'lazio.png',
  'марсель': 'marseille.png',
  'лилль': 'lille.png',
  'айнтрахт': 'eintracht.png',
  'майнц': 'mainz.png',
  'бернли': 'burnley.png',
  'будё глимт': 'bodo_glimt.png',
  'кельн': 'koln.png',
  // DIV_2
  'вулверхэмптон': 'wolverhampton.png',
  'бурирам': 'buriram.png',
  'валенсия': 'valencia.png',
  'сельта': 'celta.png',
  'ривер плейт': 'river_plate.png',
  'аякс': 'ajax.png',
  'спортинг': 'sporting.png',
  'монако': 'monaco.png',
  'бенфика': 'benfica.png',
  'фулхэм': 'fulham.png',
  'хоффенхайм': 'hoffenheim.png',
  'ланс': 'lens.png',
  'аль-кадисия': 'al_qadsiah.png',
  'торино': 'torino.png',
  'лос анджелес': 'los_angeles.png',
  'псв': 'psv.png',
  // DIV_3
  'сандерленд': 'sunderland.png',
  'ноттингем форест': 'nottingham_forest.png',
  'реал сосьедад': 'real_sociedad.png',
  'париж': 'paris_fc.png',
  'фенербахче': 'fenerbahce.png',
  'комо': 'como.png',
  'брентфорд': 'brentford.png',
  'кристал пэлас': 'crystal_palace.png',
  'аль-ахли': 'al_ahli.png',
  'лион': 'lyon.png',
  'борнмут': 'bournemouth.png',
  'аль-иттихад': 'al_ittihad.png',
  'трабзонспор': 'trabzonspor.png',
  'вильярреал': 'villarreal.png',
  'штутгарт': 'stuttgart.png',
  'болонья': 'bologna.png',
  // DIV_4
  'байя': 'bahia.png',
  'милан': 'milan.png',
  'боруссия дортмунд': 'borussia_dortmund.png',
  'интер милан': 'inter_milan.png',
  'брайтон': 'brighton.png',
  'байер': 'bayer_leverkusen.png',
  'лейпциг': 'leipzig.png',
  'эвертон': 'everton.png',
  'аталанта': 'atalanta.png',
  'астон вилла': 'aston_villa.png',
  'бешикташ': 'besiktas.png',
  'интер майами': 'inter_miami.png',
  'бетис': 'betis.png',
  'аль-хиляль': 'al_hilal.png',
  'ньюкасл': 'newcastle.png',
  'атлетик бильбао': 'athletic_bilbao.png',
  // DIV_5
  'арсенал': 'arsenal.png',
  'манчестер сити': 'manchester_city.png',
  'манчестер юнайтед': 'manchester_united.png',
  'тоттенхэм': 'tottenham.png',
  'атлетико мадрид': 'atletico_madrid.png',
  'барселона': 'barcelona.png',
  'реал мадрид': 'real_madrid.png',
  'бавария': 'bayern.png',
  'ливерпуль': 'liverpool.png',
  'челси': 'chelsea.png',
  'наполи': 'napoli.png',
  'ювентус': 'juventus.png',
  'рома': 'roma.png',
  'псж': 'psg.png',
  'галатасарай': 'galatasaray.png',
  'аль-наср': 'al_nassr.png'
};

// Латинские формы не перечисляем руками: имя файла и есть транслитерация клуба.
for (const file of new Set(Object.values(TEAM_LOGO_MAP))) {
  TEAM_LOGO_MAP[file.replace(/\.png$/, '').replace(/_/g, ' ')] = file;
}

// Независимая копия TEAM_ALIASES из club_registry.py: короткие формы, прозвища и
// транслит, которыми клубы зовут в жизни. Ключ — форма, значение — канон из карты
// выше; имена файлов тут не повторяются, чтобы переименование логотипа правилось
// в одном месте. Обе копии — эту и питоновскую — надо править вместе.
//
// Алиасы живут отдельно от TEAM_LOGO_MAP не для красоты: карта канонов — это то,
// что клуб есть, а алиас — то, как его называют. Ключ матчится только целиком,
// ровно как ALIAS-тир резолвера на бэке: «порт» ведёт в «Порту», потому что так
// записано, а не потому что он куда-то входит подстрокой.
export const TEAM_LOGO_ALIASES = {
  // DIV_1
  'лидс юнайтед': 'Лидс', 'leeds': 'Лидс', 'leeds united': 'Лидс',
  'rennes': 'Ренн', 'stade rennais': 'Ренн', 'стад ренн': 'Ренн',
  'nice': 'Ницца', 'ogc nice': 'Ницца', 'ницца огс': 'Ницца',
  'nashville': 'Нэшвилл', 'nashville sc': 'Нэшвилл',
  'порто': 'Порту', 'порт': 'Порту', 'португал': 'Порту',
  'porto': 'Порту', 'portu': 'Порту', 'fc porto': 'Порту', 'фк порту': 'Порту', 'фк порто': 'Порту',
  'west ham': 'Вест Хэм', 'west ham united': 'Вест Хэм', 'вест хем юнайтед': 'Вест Хэм',
  'wolfsburg': 'Вольфсбург', 'vfl wolfsburg': 'Вольфсбург',
  'fiorentina': 'Фиорентина', 'фиора': 'Фиорентина', 'виола': 'Фиорентина',
  'lazio': 'Лацио', 'ss lazio': 'Лацио',
  'marseille': 'Марсель', 'olympique marseille': 'Марсель', 'олимпик марсель': 'Марсель',
  'lille': 'Лилль', 'losc': 'Лилль',
  'айнтрахт франкфурт': 'Айнтрахт', 'франкфурт': 'Айнтрахт',
  'eintracht': 'Айнтрахт', 'eintracht frankfurt': 'Айнтрахт', 'frankfurt': 'Айнтрахт',
  'mainz': 'Майнц', 'mainz 05': 'Майнц', 'майнц 05': 'Майнц',
  'burnley': 'Бернли',
  'будеглимт': 'Будё Глимт', 'буде': 'Будё Глимт', 'глимт': 'Будё Глимт',
  'bodo glimt': 'Будё Глимт', 'bodoe glimt': 'Будё Глимт', 'bodo': 'Будё Глимт', 'glimt': 'Будё Глимт',
  'koln': 'Кельн', 'cologne': 'Кельн', '1 fc koln': 'Кельн',
  // DIV_2
  'вулвз': 'Вулверхэмптон', 'wolves': 'Вулверхэмптон', 'wolverhampton': 'Вулверхэмптон',
  'buriram': 'Бурирам', 'buriram united': 'Бурирам', 'бурирам юнайтед': 'Бурирам',
  'valencia': 'Валенсия', 'valencia cf': 'Валенсия',
  'celta': 'Сельта', 'celta vigo': 'Сельта', 'сельта виго': 'Сельта',
  'ривер': 'Ривер Плейт', 'плейт': 'Ривер Плейт', 'ривера': 'Ривер Плейт',
  'river plate': 'Ривер Плейт', 'river': 'Ривер Плейт',
  'аякса': 'Аякс', 'аяксу': 'Аякс', 'аяксе': 'Аякс',
  'ajax': 'Аякс', 'afc ajax': 'Аякс',
  'аякс амстердам': 'Аякс', 'ajax amsterdam': 'Аякс',
  'спортнг': 'Спортинг', 'спортинга': 'Спортинг', 'спорт': 'Спортинг',
  'sporting': 'Спортинг', 'sporting cp': 'Спортинг', 'спортинг лиссабон': 'Спортинг',
  'monaco': 'Монако', 'as monaco': 'Монако', 'ас монако': 'Монако',
  'бенфику': 'Бенфика', 'бенфике': 'Бенфика', 'бенфики': 'Бенфика', 'бенфа': 'Бенфика',
  'benfica': 'Бенфика', 'sl benfica': 'Бенфика', 'бенфика лиссабон': 'Бенфика',
  'fulham': 'Фулхэм',
  'hoffenheim': 'Хоффенхайм', 'tsg hoffenheim': 'Хоффенхайм', 'хофенхайм': 'Хоффенхайм',
  'lens': 'Ланс', 'rc lens': 'Ланс',
  'кадисия': 'Аль-Кадисия', 'al qadsiah': 'Аль-Кадисия', 'qadsiah': 'Аль-Кадисия',
  'torino': 'Торино', 'torino fc': 'Торино', 'торо': 'Торино',
  'lafc': 'Лос Анджелес', 'лафк': 'Лос Анджелес',
  'псв эйндховен': 'ПСВ', 'psv': 'ПСВ', 'psv eindhoven': 'ПСВ',
  // DIV_3
  'sunderland': 'Сандерленд',
  'ноттингем': 'Ноттингем Форест', 'форест': 'Ноттингем Форест', 'ноттингем форрест': 'Ноттингем Форест',
  'nottingham': 'Ноттингем Форест', 'nottingham forest': 'Ноттингем Форест', 'forest': 'Ноттингем Форест',
  'сосьедад': 'Реал Сосьедад', 'реал сосиедад': 'Реал Сосьедад',
  'sociedad': 'Реал Сосьедад', 'real sociedad': 'Реал Сосьедад',
  'paris fc': 'Париж', 'париж фк': 'Париж',
  'fenerbahce': 'Фенербахче', 'fener': 'Фенербахче',
  'como': 'Комо', 'como 1907': 'Комо',
  'brentford': 'Брентфорд',
  'палас': 'Кристал Пэлас', 'кристал палас': 'Кристал Пэлас',
  'palace': 'Кристал Пэлас', 'crystal palace': 'Кристал Пэлас',
  'ахли': 'Аль-Ахли', 'al ahli': 'Аль-Ахли',
  'lyon': 'Лион', 'olympique lyonnais': 'Лион', 'олимпик лион': 'Лион',
  'bournemouth': 'Борнмут',
  'иттихад': 'Аль-Иттихад', 'al ittihad': 'Аль-Иттихад',
  'трабзон': 'Трабзонспор', 'trabzon': 'Трабзонспор', 'trabzonspor': 'Трабзонспор',
  'вильяреал': 'Вильярреал', 'villarreal': 'Вильярреал',
  'stuttgart': 'Штутгарт', 'vfb stuttgart': 'Штутгарт',
  'bologna': 'Болонья',
  // DIV_4
  'баия': 'Байя', 'bahia': 'Байя', 'ec bahia': 'Байя',
  'milan': 'Милан', 'ac milan': 'Милан', 'ац милан': 'Милан',
  'дортмунд': 'Боруссия Дортмунд', 'боруссия': 'Боруссия Дортмунд', 'бвб': 'Боруссия Дортмунд',
  'dortmund': 'Боруссия Дортмунд', 'borussia dortmund': 'Боруссия Дортмунд', 'bvb': 'Боруссия Дортмунд',
  'интернационале': 'Интер Милан', 'inter milan': 'Интер Милан', 'internazionale': 'Интер Милан',
  'brighton': 'Брайтон', 'brighton hove albion': 'Брайтон',
  'леверкузен': 'Байер', 'байер леверкузен': 'Байер', 'байер 04': 'Байер',
  'leverkusen': 'Байер', 'bayer': 'Байер', 'bayer leverkusen': 'Байер',
  'leipzig': 'Лейпциг', 'rb leipzig': 'Лейпциг', 'рб лейпциг': 'Лейпциг',
  'everton': 'Эвертон',
  'atalanta': 'Аталанта', 'аталанта бергамо': 'Аталанта',
  'вилла': 'Астон Вилла', 'астон вила': 'Астон Вилла', 'villa': 'Астон Вилла', 'aston villa': 'Астон Вилла',
  'besiktas': 'Бешикташ',
  'майами': 'Интер Майами', 'интер маями': 'Интер Майами',
  'miami': 'Интер Майами', 'inter miami': 'Интер Майами',
  'betis': 'Бетис', 'real betis': 'Бетис', 'реал бетис': 'Бетис',
  'хиляль': 'Аль-Хиляль', 'аль хилаль': 'Аль-Хиляль', 'al hilal': 'Аль-Хиляль',
  'newcastle': 'Ньюкасл', 'newcastle united': 'Ньюкасл', 'ньюкасл юнайтед': 'Ньюкасл',
  'бильбао': 'Атлетик Бильбао', 'атлетик': 'Атлетик Бильбао', 'атлетик клуб': 'Атлетик Бильбао',
  'bilbao': 'Атлетик Бильбао', 'athletic': 'Атлетик Бильбао', 'athletic bilbao': 'Атлетик Бильбао',
  // DIV_5
  'arsenal': 'Арсенал',
  'ман сити': 'Манчестер Сити', 'сити': 'Манчестер Сити',
  'man city': 'Манчестер Сити', 'manchester city': 'Манчестер Сити',
  'мю': 'Манчестер Юнайтед', 'ман юнайтед': 'Манчестер Юнайтед', 'ман юнайтид': 'Манчестер Юнайтед',
  'man utd': 'Манчестер Юнайтед', 'man united': 'Манчестер Юнайтед',
  'manchester united': 'Манчестер Юнайтед',
  'шпоры': 'Тоттенхэм', 'тоттенхем хотспур': 'Тоттенхэм', 'spurs': 'Тоттенхэм', 'tottenham': 'Тоттенхэм',
  'атлетико': 'Атлетико Мадрид', 'атлети': 'Атлетико Мадрид',
  'atletico': 'Атлетико Мадрид', 'atletico madrid': 'Атлетико Мадрид',
  'барса': 'Барселона', 'барка': 'Барселона',
  'barca': 'Барселона', 'barcelona': 'Барселона', 'fc barcelona': 'Барселона',
  'real madrid': 'Реал Мадрид',
  'мюнхен': 'Бавария', 'бавария мюнхен': 'Бавария',
  'байерн': 'Бавария', 'байерн мюнхен': 'Бавария',
  'bayern': 'Бавария', 'bayern munich': 'Бавария', 'fc bayern': 'Бавария',
  'liverpool': 'Ливерпуль', 'лфк': 'Ливерпуль', 'lfc': 'Ливерпуль',
  'chelsea': 'Челси',
  'napoli': 'Наполи', 'ssc napoli': 'Наполи',
  'юве': 'Ювентус', 'juve': 'Ювентус', 'juventus': 'Ювентус',
  'roma': 'Рома', 'as roma': 'Рома', 'ас рома': 'Рома',
  'psg': 'ПСЖ', 'paris saint germain': 'ПСЖ', 'пари сен жермен': 'ПСЖ',
  'гала': 'Галатасарай', 'gala': 'Галатасарай', 'galatasaray': 'Галатасарай',
  'наср': 'Аль-Наср', 'аль насср': 'Аль-Наср', 'al nassr': 'Аль-Наср', 'al nasr': 'Аль-Наср'
};

// Как normalize_team_name на бэке: ё/э сворачиваются в е, разделители — в пробел.
// «Фулхем» и «Фулхэм» — одно и то же имя, а по ростеру эта свёртка не склеивает
// два разных клуба (проверено там же, где и для резолвера).
function normalizeLogoKey(s) {
  return s.trim().toLowerCase()
    .replace(/[ёэë]/g, 'е')
    .replace(/[øö]/g, 'o')
    .replace(/[-_./\\,]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

// Все три индекса считаются один раз на загрузку модуля: getTeamLogoUrl зовётся на
// каждую строку каждой таблицы, а карта с алиасами разрослась до трёх сотен ключей.
const LOGO_BY_KEY = new Map(
  Object.entries(TEAM_LOGO_MAP).map(([k, file]) => [normalizeLogoKey(k), file])
);

// Алиас, совпавший с каноничным именем, отбрасывается — точный тир и так его знает.
const ALIAS_INDEX = new Map();
for (const [alias, club] of Object.entries(TEAM_LOGO_ALIASES)) {
  const key = normalizeLogoKey(alias);
  if (LOGO_BY_KEY.has(key)) continue;
  const file = LOGO_BY_KEY.get(normalizeLogoKey(club));
  if (file) ALIAS_INDEX.set(key, file);
}

// Формы без пробелов — под OCR, который слепляет слова («РиверПлейт»). Ключ,
// на который претендуют два клуба, выбрасывается: победить по случайности нельзя.
const JOINED_INDEX = new Map();
const joinedConflicts = new Set();
for (const source of [LOGO_BY_KEY, ALIAS_INDEX]) {
  for (const [key, file] of source) {
    const glued = key.replace(/ /g, '');
    if (!glued || glued === key) continue;
    const existing = JOINED_INDEX.get(glued);
    if (existing !== undefined && existing !== file) joinedConflicts.add(glued);
    else JOINED_INDEX.set(glued, file);
  }
}
for (const key of joinedConflicts) JOINED_INDEX.delete(key);

// Юридические приставки: шум, а не часть имени — тот же список, что и
// _NOISE_TOKENS в club_registry.py. Географических уточнений здесь нет и быть не
// должно: именно они отличают «Расинг Сантандер» от «Расинг Ланс».
const LOGO_NOISE_TOKENS = new Set([
  'фк', 'фс', 'сп', 'сц', 'кф', 'клуб',
  'fc', 'sc', 'sl', 'cf', 'ac', 'afc', 'cp', 'club', 'jrs',
]);

// Переписи входа, которые стоят второго захода в индексы, по убыванию доверия.
function alternateLogoForms(norm) {
  const forms = [];
  const tokens = norm.split(' ');
  const stripped = tokens.filter((t) => !LOGO_NOISE_TOKENS.has(t));

  if (stripped.length && stripped.length !== tokens.length) forms.push(stripped.join(' '));
  if (tokens.length > 1) forms.push(tokens.join(''));
  if (stripped.length > 1 && stripped.length !== tokens.length) forms.push(stripped.join(''));

  return forms.filter((f) => f && f !== norm);
}

// Повторяет тиры EXACT → ALIAS → JOINED из club_registry.py и на них
// останавливается. Подстрочного тира тут нет — ровно как и на бэке, и по той же
// причине: безопасной версии у него не существует. «Юнайтед» входит в «Манчестер
// Юнайтед» и в «Ньюкасл Юнайтед», «порт» — в «Спортинг», «paris» — в «Paris FC»
// и в ПСЖ. Пустой бейдж обратим, чужой герб — нет. Короткие формы, ради которых
// проход когда-то завели, давно разобраны по TEAM_LOGO_ALIASES.
function lookupLogoFile(t) {
  const direct = LOGO_BY_KEY.get(t) || ALIAS_INDEX.get(t);
  if (direct) return direct;

  const forms = alternateLogoForms(t);
  for (const form of forms) {
    const hit = LOGO_BY_KEY.get(form) || ALIAS_INDEX.get(form);
    if (hit) return hit;
  }
  for (const form of [t, ...forms]) {
    const hit = JOINED_INDEX.get(form.replace(/ /g, ''));
    if (hit) return hit;
  }
  return null;
}

export function getTeamLogoUrl(teamName) {
  if (!teamName) return null;
  const t = normalizeLogoKey(teamName);
  if (!t) return null;

  const file = lookupLogoFile(t);
  return file ? `/assets/logos/${file}` : null;
}

// Размеры и оформление — в components.css (.team-logo-wrapper, img.team-logo-img).
// Не загрузившийся логотип удаляется: правило `img + .team-logo-fallback { display: none }`
// перестаёт совпадать, и щит-заглушка показывается средствами CSS.
const LOGO_ONERROR = 'onerror="this.remove()"';

export function renderTeamLogoWrapperHtml(teamName, extraClass = '') {
  const url = getTeamLogoUrl(teamName);
  const img = url
    ? `<img src="${url}" alt="${teamName || 'Club'}" loading="lazy" decoding="async" ${LOGO_ONERROR} />`
    : '';
  return `<div class="team-logo-wrapper ${extraClass}">${img}<span class="team-logo-fallback">🛡️</span></div>`;
}

export function renderTeamLogoHtml(teamName, size = 28, extraClass = '') {
  const url = getTeamLogoUrl(teamName);
  const fallback = `<span class="team-logo-fallback ${extraClass}" style="font-size:${Math.round(size * 0.75)}px;">🛡️</span>`;
  if (url) {
    return `<img src="${url}" alt="${teamName || 'Club'}" class="team-logo-img ${extraClass}" loading="lazy" decoding="async" style="width:${size}px; height:${size}px;" ${LOGO_ONERROR} />${fallback}`;
  }
  return fallback;
}

const OUTCOME_NAMES = {
  // Legacy / client-side keys (kept for bet slip display of old bets)
  p1: 'П1',
  x: 'Х',
  p2: 'П2',
  tb25: 'ТБ 2.5',
  tm25: 'ТМ 2.5',
  btts_yes: 'ОЗ: Да',
  btts_no: 'ОЗ: Нет',
  dc_1x: '1X',
  dc_12: '12',
  dc_x2: 'X2',
  over_15: 'ТБ 1.5',
  under_15: 'ТМ 1.5',
  over_25: 'ТБ 2.5',
  under_25: 'ТМ 2.5',
  over_35: 'ТБ 3.5',
  under_35: 'ТМ 3.5',
  // DB-side selection_key values (from odds_engine.py)
  '1x': '1X',
  '12': '12',
  'x2': 'X2',
  'over_1.5': 'ТБ 1.5',
  'under_1.5': 'ТМ 1.5',
  'over_2.5': 'ТБ 2.5',
  'under_2.5': 'ТМ 2.5',
  'over_3.5': 'ТБ 3.5',
  'under_3.5': 'ТМ 3.5',
  'h1_minus_1.5': 'Фора 1 (-1.5)',
  'h2_plus_1.5': 'Фора 2 (+1.5)',
  'h1_plus_1.5': 'Фора 1 (+1.5)',
  'h2_minus_1.5': 'Фора 2 (-1.5)',
  'it1_over_1.5': 'ИТБ1 (1.5)',
  'it1_under_1.5': 'ИТМ1 (1.5)',
  'it2_over_1.5': 'ИТБ2 (1.5)',
  'it2_under_1.5': 'ИТМ2 (1.5)'
};

/** «1/64 финала», «Финал» — подпись этапа кубка. */
export function cupStageLabel(stage) {
  if (!stage) return 'Кубок';
  return stage === 'final' ? 'Финал' : `${stage} финала`;
}

/**
 * «Тур 3» / «1/64 финала · игра 2» — подпись матча. У кубковой игры
 * round_number = -1, это не номер тура. Кабинет отдаёт is_cup/game_num,
 * /api/matches/{id} — сырые tournament_type/game_num_in_series.
 */
export function matchRoundLabel(m, fallback = 'Матч') {
  if (m?.is_cup || m?.tournament_type === 'cup') {
    const stage = cupStageLabel(m.cup_stage);
    const game = m.game_num ?? m.game_num_in_series;
    return game ? `${stage} · игра ${game}` : stage;
  }
  const round = Number(m?.round_number);
  return round > 0 ? `Тур ${round}` : fallback;
}

/** Market title for a slip item added without one (Line-tab tiles, old drafts). */
export function marketNameForOutcome(key) {
  const k = String(key || '').toLowerCase();
  if (['p1', 'x', 'p2'].includes(k)) return 'Исход матча';
  if (k.startsWith('dc_') || ['1x', '12', 'x2'].includes(k)) return 'Двойной шанс';
  if (k.startsWith('btts')) return 'Обе забьют';
  if (/^h[12]_/.test(k)) {
    const m = k.match(/_(plus|minus)_([\d.]+)$/);
    return m ? `Фора (${m[1] === 'plus' ? '+' : '-'}${m[2]})` : 'Фора';
  }
  if (k.startsWith('it1') || k.startsWith('it2')) return 'Инд. тотал';
  if (/^(tb|tm|over|under)/.test(k)) return 'Тотал матча';
  return 'Рынок';
}

/**
 * Имена игроков и ники соперников приходят из пользовательского ввода и OCR,
 * поэтому перед вставкой в innerHTML их обязательно экранировать.
 */
// ─── Длинные списки ───
// История прогнозов, архив результатов и достижения строятся порциями: сразу — первые
// LIST_PAGE_SIZE карточек, остальные — по кнопке «Показать ещё». Раскрытое число
// запоминается по ключу списка, чтобы фоновое обновление данных не сворачивало его обратно.
const LIST_PAGE_SIZE = 20;
const listShownCounts = new Map();

export function renderPagedList(container, key, items, renderItem, pageSize = LIST_PAGE_SIZE) {
  const total = items.length;
  let shown = Math.min(total, Math.max(pageSize, listShownCounts.get(key) || 0));
  container.innerHTML = items.slice(0, shown).map(renderItem).join('');
  if (shown >= total) return;

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'list-more-btn';
  const label = () => { btn.textContent = `Показать ещё (${total - shown})`; };
  label();
  btn.addEventListener('click', () => {
    const next = Math.min(total, shown + pageSize);
    btn.insertAdjacentHTML('beforebegin', items.slice(shown, next).map(renderItem).join(''));
    shown = next;
    listShownCounts.set(key, shown);
    if (shown >= total) btn.remove();
    else label();
  });
  container.appendChild(btn);
}

export function escapeHtml(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export class UIRenderer {
  static formatNumber(n) {
    return (n || 0).toLocaleString('ru-RU');
  }

  static renderHeader(user, progression, unclaimedAchievements = 0) {
    const balEl = document.getElementById('user-balance-val');
    if (balEl && user) {
      // Монета уже нарисована в .balance-icon — второй эмодзи здесь не нужен.
      balEl.textContent = this.formatNumber(user.balance);
    }
    const lvlEl = document.getElementById('user-level-val');
    if (lvlEl && progression) {
      lvlEl.textContent = `Lvl ${progression.level || 1}`;
    }

    // Точка на вкладке профиля: есть открытые достижения, награда за которые
    // ещё не забрана.
    const aBadge = document.getElementById('achievements-badge');
    if (aBadge) aBadge.style.display = unclaimedAchievements > 0 ? '' : 'none';
  }

  static updateNavClubIcon(overview) {
    const navIcon = document.getElementById('nav-my-club-icon') || document.querySelector('.nav-item[data-view="my_club"] .nav-icon');
    if (!navIcon) return;

    const teamName = overview?.registered && overview?.club?.team_name;
    if (!teamName) {
      if (navIcon.dataset.currentTeam) {
        delete navIcon.dataset.currentTeam;
        navIcon.textContent = '🛡';
      }
      return;
    }

    const logoUrl = getTeamLogoUrl(teamName);
    if (!logoUrl) {
      if (navIcon.dataset.currentTeam) {
        delete navIcon.dataset.currentTeam;
        navIcon.textContent = '🛡';
      }
      return;
    }

    if (navIcon.dataset.currentTeam !== teamName) {
      navIcon.dataset.currentTeam = teamName;
      navIcon.innerHTML = `<img src="${logoUrl}" alt="${escapeHtml(teamName)}" class="nav-club-logo" width="22" height="22" onerror="var p=this.parentElement; if(!p) return; p.textContent='🛡'; delete p.dataset.currentTeam;" />`;
    }
  }

  static renderDivisionTabs(divisions, selectedDivisionId, containerId = 'lobby-division-tabs-container', lobbyMode = null) {
    const container = document.getElementById(containerId);
    if (!container) return;

    const divs = (divisions && divisions.length > 0) ? divisions : [
      { id: 1, name: 'Дивизион 1' },
      { id: 2, name: 'Дивизион 2' },
      { id: 3, name: 'Дивизион 3' },
      { id: 4, name: 'Дивизион 4' },
      { id: 5, name: 'Дивизион 5' }
    ];

    // Чип кубка есть только в лобби: турнирные таблицы кубка не показывают.
    const isCup = lobbyMode === 'cup';
    const cupChip = lobbyMode !== null ? `
      <button class="division-tab-btn cup-tab-btn ${isCup ? 'active' : ''}" data-cup-tab="1">
        🏆 Кубок
      </button>
    ` : '';

    container.innerHTML = cupChip + divs.map(d => `
      <button class="division-tab-btn ${!isCup && d.id === selectedDivisionId ? 'active' : ''}" 
              data-division-id="${d.id}">
        🛡️ ${d.name || `Дивизион ${d.id}`}
      </button>
    `).join('');
  }

  /**
   * Сводит матчи всех туров дивизиона в один плоский список линии.
   * Пропускаем только то, на что реально можно поставить:
   *  - статус не «сыгран» (confirmed / completed / finished);
   *  - бэкенд не пометил матч архивным (is_line === false);
   *  - есть действующие коэффициенты (заглушки архива приходят как 1.0).
   */
  static collectLineMatches(tours) {
    const FINISHED = ['confirmed', 'completed', 'finished', 'cancelled'];
    const ACTIVE = ['pending', 'open', 'scheduled', 'live'];

    const flat = [];
    for (const t of (tours || [])) {
      for (const m of (t.matches || [])) {
        const status = (m.status || 'pending').toLowerCase();
        if (FINISHED.includes(status)) continue;
        if (!ACTIVE.includes(status)) continue;
        if (m.is_line === false) continue;

        const o = m.odds || {};
        const hasRealOdds = [o.p1, o.x, o.p2].every(v => typeof v === 'number' && v > 1.0);
        if (!hasRealOdds) continue;

        flat.push({ ...m, tour: m.tour || t.round_number });
      }
    }

    flat.sort((a, b) => (a.tour - b.tour) || (a.match_id - b.match_id));
    return flat;
  }

  static renderMatches(tours, activeCategory = 'all', searchQuery = '', selectedDivisionId = 1) {
    const container = document.getElementById('matches-list-container');
    if (!container) return;

    // Линия — единый сквозной список по всем турам дивизиона: только те матчи,
    // которые реально открыты для ставок. Завершённые игры живут в «Турнирах».
    const lineMatches = UIRenderer.collectLineMatches(tours);

    if (lineMatches.length === 0) {
      container.innerHTML = `
        <div class="line-empty">
          <div class="line-empty-icon">🏆</div>
          <div class="line-empty-title">Сейчас нет открытых матчей для ставок</div>
          <div class="line-empty-hint">Ожидайте открытия линии</div>
        </div>
      `;
      return;
    }

    let filteredMatches = lineMatches;

    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase().trim();
      filteredMatches = filteredMatches.filter(m => 
        (m.team1_name || '').toLowerCase().includes(q) || 
        (m.team2_name || '').toLowerCase().includes(q)
      );
    }

    if (filteredMatches.length === 0) {
      container.innerHTML = `
        <div class="line-empty-search">
          В открытой линии нет матчей по запросу «${searchQuery}»
        </div>
      `;
      return;
    }

    container.innerHTML = filteredMatches.map(m => {
      const isLive = m.status === 'live';
      const tourLabel = m.tour;
      const divLabel = m.division_id || selectedDivisionId || 1;
      const u1 = (m.player1_username || m.player1_nickname || '').trim();
      const tag1 = u1 ? (u1.startsWith('@') ? u1 : `@${u1}`) : '';
      const u2 = (m.player2_username || m.player2_nickname || '').trim();
      const tag2 = u2 ? (u2.startsWith('@') ? u2 : `@${u2}`) : '';

      return `
        <div class="match-card" data-match-id="${m.match_id}">
          <!-- Match Card Header -->
          <div class="match-card-header">
            <div class="match-card-tags">
              <span class="match-division-tag division-tag-gold">
                Дивизион ${divLabel}
              </span>
              <span class="match-tour-tag">Тур ${tourLabel}</span>
              ${isLive ? `
                <span class="live-badge">
                  <span class="live-dot"></span> LIVE ${m.live_minute ? `${m.live_minute}'` : ''}
                </span>
              ` : ''}
            </div>
            <span class="match-league-label">Лига Фифарей</span>
          </div>

          <!-- Teams Row with Crest Logos (Horizontal Centered Layout) -->
          <div class="match-teams-row">
            <div class="team-block-side left">
              <div class="team-meta-wrap left">
                <span class="team-name" title="${escapeHtml(m.team1_name)}">${escapeHtml(m.team1_name)}</span>
                ${tag1 ? `<span class="team-player-tag" title="${escapeHtml(tag1)}">${escapeHtml(tag1)}</span>` : ''}
              </div>
              ${renderTeamLogoHtml(m.team1_name, 28)}
            </div>
            <div class="match-vs-divider">VS</div>
            <div class="team-block-side right">
              ${renderTeamLogoHtml(m.team2_name, 28)}
              <div class="team-meta-wrap right">
                <span class="team-name" title="${escapeHtml(m.team2_name)}">${escapeHtml(m.team2_name)}</span>
                ${tag2 ? `<span class="team-player-tag" title="${escapeHtml(tag2)}">${escapeHtml(tag2)}</span>` : ''}
              </div>
            </div>
          </div>

          <!-- Primary 1X2 Odds Buttons Grid -->
          <div class="odds-grid-3col">
            <div class="odd-btn ${store.isSelectionActive(m.match_id, 'p1') ? 'selected' : ''}" 
                 data-match-id="${m.match_id}" data-outcome="p1" data-odd="${m.odds?.p1 || 1.90}">
              <span class="odd-label">П1</span>
              <span class="odd-val">${(m.odds?.p1 || 1.90).toFixed(2)}</span>
            </div>
            <div class="odd-btn ${store.isSelectionActive(m.match_id, 'x') ? 'selected' : ''}" 
                 data-match-id="${m.match_id}" data-outcome="x" data-odd="${m.odds?.x || 3.20}">
              <span class="odd-label">X</span>
              <span class="odd-val">${(m.odds?.x || 3.20).toFixed(2)}</span>
            </div>
            <div class="odd-btn ${store.isSelectionActive(m.match_id, 'p2') ? 'selected' : ''}" 
                 data-match-id="${m.match_id}" data-outcome="p2" data-odd="${m.odds?.p2 || 2.40}">
              <span class="odd-label">П2</span>
              <span class="odd-val">${(m.odds?.p2 || 2.40).toFixed(2)}</span>
            </div>
          </div>

          <!-- Secondary Filtered Category Odds (if chosen) -->
          ${(activeCategory === 'totals') ? `
            <div class="odds-grid-2col">
              <div class="odd-btn ${store.isSelectionActive(m.match_id, 'tb25') ? 'selected' : ''}" 
                   data-match-id="${m.match_id}" data-outcome="tb25" data-odd="${m.odds?.tb25 || 1.80}">
                <span class="odd-label">ТБ 2.5</span>
                <span class="odd-val">${(m.odds?.tb25 || 1.80).toFixed(2)}</span>
              </div>
              <div class="odd-btn ${store.isSelectionActive(m.match_id, 'tm25') ? 'selected' : ''}" 
                   data-match-id="${m.match_id}" data-outcome="tm25" data-odd="${m.odds?.tm25 || 1.95}">
                <span class="odd-label">ТМ 2.5</span>
                <span class="odd-val">${(m.odds?.tm25 || 1.95).toFixed(2)}</span>
              </div>
            </div>
          ` : ''}

          ${(activeCategory === 'btts') ? `
            <div class="odds-grid-2col">
              <div class="odd-btn ${store.isSelectionActive(m.match_id, 'btts_yes') ? 'selected' : ''}" 
                   data-match-id="${m.match_id}" data-outcome="btts_yes" data-odd="${m.odds?.btts_yes || 1.70}">
                <span class="odd-label">ОЗ Да</span>
                <span class="odd-val">${(m.odds?.btts_yes || 1.70).toFixed(2)}</span>
              </div>
              <div class="odd-btn ${store.isSelectionActive(m.match_id, 'btts_no') ? 'selected' : ''}" 
                   data-match-id="${m.match_id}" data-outcome="btts_no" data-odd="${m.odds?.btts_no || 2.05}">
                <span class="odd-label">ОЗ Нет</span>
                <span class="odd-val">${(m.odds?.btts_no || 2.05).toFixed(2)}</span>
              </div>
            </div>
          ` : ''}

          <!-- Match Navigation Action Buttons -->
          <div class="match-card-actions">
            <button class="btn-match-action btn-open-match-center" data-match-id="${m.match_id}">
              📊 Статистика & H2H
            </button>
            <button class="btn-match-action accent btn-more-markets" data-match-id="${m.match_id}">
              ⚡ Все рынки (15+)
            </button>
          </div>
        </div>
      `;
    }).join('');
  }

  /** Лобби в режиме кубка прячет линию дивизиона и хабы лиги. */
  static renderLobbyMode(mode) {
    const isCup = mode === 'cup';
    ['matches-list-container', 'hot-matches-container', 'odds-movers-container', 'recommendations-container']
      .forEach(id => {
        const el = document.getElementById(id);
        if (el) el.style.display = isCup ? 'none' : '';
      });
    const cupEl = document.getElementById('cup-view-container');
    if (cupEl) cupEl.style.display = isCup ? '' : 'none';
  }

  /**
   * Кнопка исхода кубковой линии. Без живого коэффициента — неактивная плашка,
   * а не выдуманное число: закрытую линию сервер отдаёт с пустыми `odds`.
   */
  static _cupOddBtn(tile, key, label, meta, names = {}) {
    const odd = tile?.odds?.[key];
    if (!tile || tile.is_line === false || typeof odd !== 'number' || odd <= 1.0) {
      return `
        <div class="cup-odd-locked">
          <span class="odd-label">${escapeHtml(label)}</span>
          <span class="odd-val">—</span>
        </div>`;
    }
    const active = store.isSelectionActive(tile.match_id, key);
    return `
      <div class="odd-btn ${active ? 'selected' : ''}"
           data-match-id="${tile.match_id}" data-outcome="${key}" data-odd="${odd}"
           data-slip-meta="${escapeHtml(meta)}"
           ${names.market ? `data-market-name="${escapeHtml(names.market)}"` : ''}
           ${names.selection ? `data-selection-name="${escapeHtml(names.selection)}"` : ''}>
        <span class="odd-label">${escapeHtml(label)}</span>
        <span class="odd-val">${odd.toFixed(2)}</span>
      </div>`;
  }

  static _cupPlayerTag(username) {
    const u = (username || '').trim();
    if (!u) return '';
    const tag = u.startsWith('@') ? u : `@${u}`;
    return `<span class="team-player-tag" title="${escapeHtml(tag)}">${escapeHtml(tag)}</span>`;
  }

  static _cupTeamsRow(s, { withScore = false } = {}) {
    const t1 = s.team1_name;
    const t2 = s.team2_name;
    const winner = withScore ? s.winner_name : null;
    const hasScore = withScore && s.team1_wins != null && s.team2_wins != null;
    const cls = (name) => (winner ? (name === winner ? 'cup-team winner' : 'cup-team loser') : 'cup-team');
    return `
      <div class="match-teams-row">
        <div class="team-block-side left">
          <div class="team-meta-wrap left">
            <span class="team-name ${cls(t1)}" title="${escapeHtml(t1)}">${escapeHtml(t1)}</span>
            ${UIRenderer._cupPlayerTag(s.team1_username)}
          </div>
          ${renderTeamLogoHtml(t1, 28)}
        </div>
        <div class="match-vs-divider ${hasScore ? 'cup-series-score' : ''}">${hasScore ? `${s.team1_wins} : ${s.team2_wins}` : 'VS'}</div>
        <div class="team-block-side right">
          ${renderTeamLogoHtml(t2, 28)}
          <div class="team-meta-wrap right">
            <span class="team-name ${cls(t2)}" title="${escapeHtml(t2)}">${escapeHtml(t2)}</span>
            ${UIRenderer._cupPlayerTag(s.team2_username)}
          </div>
        </div>
      </div>`;
  }

  static renderCupView(cup, searchQuery = '') {
    const container = document.getElementById('cup-view-container');
    if (!container) return;
    const stages = cup?.stages || [];

    if (cup?.loading && stages.length === 0) {
      container.innerHTML = `<div class="cup-empty"><div class="cup-empty-icon">⏳</div>Загрузка кубка...</div>`;
      return;
    }
    if (stages.length === 0) {
      container.innerHTML = `
        <div class="cup-empty">
          <div class="cup-empty-icon">🏆</div>
          <div class="cup-empty-title">${cup?.error ? escapeHtml(cup.error) : 'Сетка кубка ещё не сформирована'}</div>
          <div>Линия появится, когда администратор откроет ставки на этап.</div>
        </div>`;
      return;
    }

    const selectedId = cup.selectedStageId;
    const stageChips = stages.map(s => `
      <button class="cup-stage-chip ${s.id === selectedId ? 'active' : ''}" data-stage-id="${s.id}">
        ${s.bets_open ? '<span class="cup-stage-dot" title="Ставки открыты"></span>' : ''}
        ${escapeHtml(cupStageLabel(s.stage))}
        ${s.series_total ? `<span class="cup-stage-count">${s.series_completed}/${s.series_total}</span>` : ''}
      </button>`).join('');

    const view = cup.view === 'bracket' ? 'bracket' : 'line';
    const toggle = `
      <div class="cup-view-toggle">
        <button class="cup-view-btn ${view === 'line' ? 'active' : ''}" data-cup-view="line">Линия</button>
        <button class="cup-view-btn ${view === 'bracket' ? 'active' : ''}" data-cup-view="bracket">Сетка</button>
      </div>`;

    let body;
    if (cup.loading) {
      body = `<div class="cup-empty"><div class="cup-empty-icon">⏳</div>Загрузка этапа...</div>`;
    } else if (cup.error) {
      body = `<div class="cup-empty cup-error">${escapeHtml(cup.error)}</div>`;
    } else {
      body = view === 'bracket'
        ? UIRenderer._renderCupBracket(cup.bracket, searchQuery)
        : UIRenderer._renderCupLine(cup.line, searchQuery);
    }

    container.innerHTML = `
      <div class="cup-stage-chips scroll-row">${stageChips}</div>
      ${toggle}
      ${body}`;
  }

  static _cupSeriesFilter(series, searchQuery) {
    const q = (searchQuery || '').toLowerCase().trim();
    if (!q) return series;
    return series.filter(s =>
      (s.team1_name || '').toLowerCase().includes(q) ||
      (s.team2_name || '').toLowerCase().includes(q));
  }

  static _renderCupLine(line, searchQuery) {
    if (!line) return '';
    const stage = line.stage || {};
    const label = cupStageLabel(stage.stage);
    const all = line.series || [];
    const series = UIRenderer._cupSeriesFilter(all, searchQuery);
    const notice = stage.bets_open ? '' : `
      <div class="cup-line-notice">🔒 Ставки на этап «${escapeHtml(label)}» сейчас не принимаются</div>`;

    if (all.length === 0) {
      return `${notice}<div class="cup-empty"><div class="cup-empty-icon">🏆</div>В этом этапе пока нет пар</div>`;
    }
    if (series.length === 0) {
      return `<div class="cup-empty">Нет пар по запросу «${escapeHtml(searchQuery)}»</div>`;
    }

    return notice + series.map(s => {
      const t1 = s.team1_name;
      const t2 = s.team2_name;
      const h = s.header ? { ...s.header, team1_name: t1, team2_name: t2 } : null;
      const seriesMeta = `Кубок · ${label} · серия`;
      const headerBlock = h ? `
        <div class="cup-market-title">Проход дальше</div>
        <div class="odds-grid-2col">
          ${UIRenderer._cupOddBtn(h, 'p1', 'П1', seriesMeta, { market: 'Проход в следующий раунд', selection: `Проходит ${t1}` })}
          ${UIRenderer._cupOddBtn(h, 'p2', 'П2', seriesMeta, { market: 'Проход в следующий раунд', selection: `Проходит ${t2}` })}
        </div>
        <div class="cup-market-title">Будет ли третья игра</div>
        <div class="odds-grid-2col">
          ${UIRenderer._cupOddBtn(h, 'tb25', 'Да', seriesMeta, { market: 'Третья игра', selection: 'Будет' })}
          ${UIRenderer._cupOddBtn(h, 'tm25', 'Нет', seriesMeta, { market: 'Третья игра', selection: 'Не будет' })}
        </div>` : '';

      const games = (s.games || []).map(g => {
        const tile = { ...g, team1_name: t1, team2_name: t2 };
        const n = g.game_num_in_series;
        const meta = `Кубок · ${label} · игра ${n}`;
        const market = `Игра ${n}: исход`;
        return `
          <div class="cup-game-row">
            <div class="cup-game-num">Игра ${n}</div>
            <div class="cup-game-odds">
              ${UIRenderer._cupOddBtn(tile, 'p1', 'П1', meta, { market })}
              ${UIRenderer._cupOddBtn(tile, 'p2', 'П2', meta, { market })}
              ${UIRenderer._cupOddBtn(tile, 'tb25', 'ТБ 2.5', meta)}
              ${UIRenderer._cupOddBtn(tile, 'tm25', 'ТМ 2.5', meta)}
            </div>
            ${g.is_line !== false ? `<button class="cup-game-more btn-more-markets" data-match-id="${g.match_id}" aria-label="Все рынки игры">+</button>` : ''}
          </div>`;
      }).join('');

      return `
        <div class="match-card cup-series-card" data-series-id="${s.series_id}">
          <div class="match-card-header">
            <div class="match-card-tags">
              <span class="match-tour-tag">🏆 ${escapeHtml(label)}</span>
              <span class="cup-series-num">Серия ${s.series_num} · до 2 побед</span>
            </div>
          </div>
          ${UIRenderer._cupTeamsRow(s)}
          ${headerBlock}
          ${games ? `<div class="cup-market-title">Игры серии</div>${games}` : ''}
          ${h && h.is_line !== false ? `
            <div class="match-card-actions">
              <button class="btn-match-action accent btn-more-markets" data-match-id="${h.match_id}">
                ⚡ Все рынки серии
              </button>
            </div>` : ''}
        </div>`;
    }).join('');
  }

  static _renderCupBracket(bracket, searchQuery) {
    if (!bracket) return '';
    const all = bracket.series || [];
    if (all.length === 0) {
      return `<div class="cup-empty"><div class="cup-empty-icon">🏆</div>В этом этапе пока нет пар</div>`;
    }
    const series = UIRenderer._cupSeriesFilter(all, searchQuery);
    if (series.length === 0) {
      return `<div class="cup-empty">Нет пар по запросу «${escapeHtml(searchQuery)}»</div>`;
    }
    const STATUS = { completed: 'Серия завершена', active: 'Идёт серия' };
    return series.map(s => {
      const games = (s.games || []).map(g => {
        const played = g.score1 !== null && g.score1 !== undefined && g.score2 !== null && g.score2 !== undefined;
        const voided = g.status === 'cancelled';
        const score = voided ? 'не игралась' : (played ? `${g.score1} : ${g.score2}` : '—');
        return `
          <div class="cup-bracket-game ${voided ? 'voided' : ''}">
            <span>Игра ${g.game_num}</span>
            <span class="cup-bracket-game-score">${escapeHtml(score)}</span>
            <span class="cup-bracket-game-winner">${g.winner_team ? escapeHtml(g.winner_team) : ''}</span>
          </div>`;
      }).join('');
      return `
        <div class="match-card cup-series-card ${s.status === 'completed' ? 'completed' : ''}">
          <div class="match-card-header">
            <span class="cup-series-num">Серия ${s.series_num}</span>
            <span class="cup-series-status">${escapeHtml(STATUS[s.status] || s.status || '')}</span>
          </div>
          ${UIRenderer._cupTeamsRow(s, { withScore: true })}
          ${games ? `<div class="cup-bracket-games">${games}</div>` : ''}
          ${s.winner_name ? `<div class="cup-series-winner">Проходит: ${escapeHtml(s.winner_name)}</div>` : ''}
        </div>`;
    }).join('');
  }

  static renderMatchCenter(matchDetail, stats, h2h, insights, live, markets = [], activeSubTab = 'markets') {
    const container = document.getElementById('match-center-container');
    if (!container) return;

    if (!matchDetail) {
      container.innerHTML = `
        <div class="mc-empty">
          <div class="mc-empty-icon">⚽</div>
          <div class="mc-empty-title">Матч не выбран</div>
          <div class="mc-empty-hint">Выберите матч из линии для просмотра коэффициентов и статистики</div>
        </div>
      `;
      return;
    }

    const t1 = matchDetail.team1_name || matchDetail.player1_team || 'Хозяева';
    const t2 = matchDetail.team2_name || matchDetail.player2_team || 'Гости';
    // live.score* берём только для реально идущего матча: у несыгранного endpoint
    // отдаёт 0 вместо NULL, и счёт «-» превратился бы в «0 : 0».
    // (До исправления контракта /live клиент вообще не признавал ответ успешным,
    //  поэтому live всегда был null и ветка не работала.)
    const liveNow = live?.match_status === 'live' ? live : null;
    const s1 = liveNow?.score1 ?? matchDetail.player1_score ?? '-';
    const s2 = liveNow?.score2 ?? matchDetail.player2_score ?? '-';
    const matchId = matchDetail.id || matchDetail.match_id;
    const roundLabel = escapeHtml(matchRoundLabel(matchDetail, 'Тур 1'));

    const t1Form = stats?.team1?.stats?.form || ['W', 'D', 'W'];
    const t2Form = stats?.team2?.stats?.form || ['D', 'L', 'W'];
    const u1 = (matchDetail.player1_username || matchDetail.player1_nickname || '').trim();
    const tag1 = u1 ? (u1.startsWith('@') ? u1 : `@${u1}`) : '';
    const u2 = (matchDetail.player2_username || matchDetail.player2_nickname || '').trim();
    const tag2 = u2 ? (u2.startsWith('@') ? u2 : `@${u2}`) : '';
    // Корона есть только у разобранного матча — у линии и лайва её быть не может.
    const mvp = (matchDetail.mvp_player || '').trim();

    container.innerHTML = `
      <!-- Header Hero Card with Clean Logos -->
      <div class="match-center-header">
        <div class="team-vs-display">
          <div class="team-block">
            <div class="team-crest-container">
              ${renderTeamLogoHtml(t1, 48, 'team-crest-img')}
            </div>
            <div class="team-name-lg mc-team-name">${escapeHtml(t1)}</div>
            ${tag1 ? `<div class="mc-team-tag">${escapeHtml(tag1)}</div>` : ''}
            <div class="form-badges-row">
              ${t1Form.map(f => `<span class="form-dot ${f.toLowerCase()}">${f}</span>`).join('')}
            </div>
          </div>
          <div class="mc-score-col">
            <div class="score-center-badge">${s1} : ${s2}</div>
            <span class="mc-status">
              ${matchDetail.status === 'live' ? '🔴 LIVE' : roundLabel}
            </span>
            ${mvp ? `<span class="mc-mvp" title="Игрок матча">👑 ${escapeHtml(mvp)}</span>` : ''}
          </div>
          <div class="team-block">
            <div class="team-crest-container">
              ${renderTeamLogoHtml(t2, 48, 'team-crest-img')}
            </div>
            <div class="team-name-lg mc-team-name">${escapeHtml(t2)}</div>
            ${tag2 ? `<div class="mc-team-tag">${escapeHtml(tag2)}</div>` : ''}
            <div class="form-badges-row">
              ${t2Form.map(f => `<span class="form-dot ${f.toLowerCase()}">${f}</span>`).join('')}
            </div>
          </div>
        </div>
      </div>

      <!-- Dedicated Sub-Navigation Menu for this Match -->
      <div class="mc-tabs">
        <button class="mc-subtab-btn ${activeSubTab === 'markets' ? 'active' : ''}" data-subtab="markets">
          🎯 Ставки и Рынки
        </button>
        <button class="mc-subtab-btn ${activeSubTab === 'stats' ? 'active' : ''}" data-subtab="stats">
          📊 Статистика & H2H
        </button>
        <button class="mc-subtab-btn ${activeSubTab === 'insights' ? 'active' : ''}" data-subtab="insights">
          🔥 Инсайты
        </button>
      </div>

      <!-- Sub-Tab Content -->
      ${activeSubTab === 'markets' ? `
        <!-- Betting Markets (server-authoritative odds) -->
        <div class="match-markets-container">
          ${(() => {
            // Helper: find a market by key from the markets array
            const findMkt = (key) => (markets || []).find(m => m.market_key === key);
            const renderSelBtn = (mkt, selKey, labelFallback, oddFallback) => {
              if (!mkt) {
                // Fallback tile if market not generated yet
                return `
                  <div class="odd-btn" data-match-id="${matchId}" data-outcome="${selKey}" data-odd="${oddFallback}">
                    <span class="odd-label">${labelFallback}</span>
                    <span class="odd-val">${Number(oddFallback).toFixed(2)}</span>
                  </div>`;
              }
              const sel = (mkt.selections || []).find(s => s.selection_key === selKey);
              if (!sel) return '';
              const odd = sel.current_odd || sel.odds_value || oddFallback;
              const isSelected = store.isSelectionActive(matchId, selKey);
              return `
                <div class="odd-btn ${isSelected ? 'selected' : ''}"
                     data-match-id="${matchId}"
                     data-outcome="${selKey}"
                     data-odd="${odd}"
                     data-market-id="${mkt.id || ''}"
                     data-selection-id="${sel.id || ''}"
                     data-market-name="${escapeHtml(mkt.market_name || '')}"
                     data-selection-name="${escapeHtml(sel.selection_name || labelFallback)}">
                  <span class="odd-label">${sel.selection_name || labelFallback}</span>
                  <span class="odd-val">${Number(odd).toFixed(2)}</span>
                </div>`;
            };
            const mkt1x2 = findMkt('1x2');
            const mktDC  = findMkt('double_chance');
            const mktTot = findMkt('total_goals');
            const mktBTTS = findMkt('btts');
            const mktHcp = findMkt('handicap');
            const mktIT1 = findMkt('individual_total_1');
            const mktIT2 = findMkt('individual_total_2');

            const noMarketsNote = (!markets || markets.length === 0)
              ? `<div class="mc-markets-pending">⏳ Рынки формируются...</div>`
              : '';

            return `
              ${noMarketsNote}
              <!-- 1X2 Main Outcomes -->
              <div class="market-group-card">
                <div class="market-group-title">⚡ Основные исходы (1X2)</div>
                <div class="odds-grid-3col">
                  ${renderSelBtn(mkt1x2, 'p1', `П1 (${t1})`, 1.90)}
                  ${renderSelBtn(mkt1x2, 'x', 'Ничья (X)', 3.20)}
                  ${renderSelBtn(mkt1x2, 'p2', `П2 (${t2})`, 2.10)}
                </div>
              </div>

              <!-- Double Chance -->
              ${mktDC ? `
              <div class="market-group-card">
                <div class="market-group-title">🔄 Двойной шанс</div>
                <div class="odds-grid-3col">
                  ${renderSelBtn(mktDC, '1x', '1X', 1.30)}
                  ${renderSelBtn(mktDC, '12', '12', 1.25)}
                  ${renderSelBtn(mktDC, 'x2', 'X2', 1.45)}
                </div>
              </div>` : ''}

              <!-- Over / Under Totals -->
              ${mktTot ? `
              <div class="market-group-card">
                <div class="market-group-title">⚽ Тоталы матча</div>
                <div class="odds-grid-2col">
                  ${renderSelBtn(mktTot, 'over_1.5', 'ТБ 1.5', 1.28)}
                  ${renderSelBtn(mktTot, 'under_1.5', 'ТМ 1.5', 3.40)}
                  ${renderSelBtn(mktTot, 'over_2.5', 'ТБ 2.5', 1.80)}
                  ${renderSelBtn(mktTot, 'under_2.5', 'ТМ 2.5', 1.95)}
                  ${renderSelBtn(mktTot, 'over_3.5', 'ТБ 3.5', 2.85)}
                  ${renderSelBtn(mktTot, 'under_3.5', 'ТМ 3.5', 1.38)}
                </div>
              </div>` : ''}

              <!-- Both Teams To Score -->
              ${mktBTTS ? `
              <div class="market-group-card">
                <div class="market-group-title">🥅 Обе команды забьют</div>
                <div class="odds-grid-2col">
                  ${renderSelBtn(mktBTTS, 'btts_yes', 'ОЗ: Да', 1.68)}
                  ${renderSelBtn(mktBTTS, 'btts_no', 'ОЗ: Нет', 2.05)}
                </div>
              </div>` : ''}

              <!-- Handicap -->
              ${mktHcp ? `
              <div class="market-group-card">
                <div class="market-group-title">↔️ Фора (±1.5)</div>
                <div class="odds-grid-2col">
                  ${(mktHcp.selections && mktHcp.selections.length > 0)
                    ? mktHcp.selections.map(s => renderSelBtn(mktHcp, s.selection_key, s.selection_name, s.current_odd || s.odds_value || 1.85)).join('')
                    : `
                      ${renderSelBtn(mktHcp, 'h1_minus_1.5', 'Фора 1 (-1.5)', 2.20)}
                      ${renderSelBtn(mktHcp, 'h2_plus_1.5', 'Фора 2 (+1.5)', 1.60)}
                    `
                  }
                </div>
              </div>` : ''}

              <!-- Individual Totals -->
              ${(mktIT1 || mktIT2) ? `
              <div class="market-group-card">
                <div class="market-group-title">🎯 Индивидуальные тоталы</div>
                <div class="odds-grid-2col">
                  ${mktIT1 ? renderSelBtn(mktIT1, 'it1_over_1.5', `ИТБ1 (1.5)`, 1.85) : ''}
                  ${mktIT1 ? renderSelBtn(mktIT1, 'it1_under_1.5', `ИТМ1 (1.5)`, 1.85) : ''}
                  ${mktIT2 ? renderSelBtn(mktIT2, 'it2_over_1.5', `ИТБ2 (1.5)`, 1.85) : ''}
                  ${mktIT2 ? renderSelBtn(mktIT2, 'it2_under_1.5', `ИТМ2 (1.5)`, 1.85) : ''}
                </div>
              </div>` : ''}
            `;
          })()}
        </div>
      ` : activeSubTab === 'stats' ? `
        <!-- Statistics & H2H Menu -->
        <div class="match-stats-container">
          <!-- Head-to-Head Section -->
          ${h2h?.summary ? `
            <div class="market-group-card">
              <div class="market-group-title">🤝 История Очных Встреч (H2H)</div>
              <div class="h2h-summary-head">
                <span>Побед ${t1}: ${h2h.summary.team1_wins}</span>
                <span>Ничьих: ${h2h.summary.draws}</span>
                <span>Побед ${t2}: ${h2h.summary.team2_wins}</span>
              </div>
              <div class="h2h-progress-bar">
                <div class="h2h-bar-p1" style="width: ${(h2h.summary.team1_wins / Math.max(1, h2h.summary.total_meetings)) * 100}%"></div>
                <div class="h2h-bar-x" style="width: ${(h2h.summary.draws / Math.max(1, h2h.summary.total_meetings)) * 100}%"></div>
                <div class="h2h-bar-p2" style="width: ${(h2h.summary.team2_wins / Math.max(1, h2h.summary.total_meetings)) * 100}%"></div>
              </div>
            </div>
          ` : ''}

          <!-- Goals Analytics -->
          ${stats?.team1?.stats ? `
            <div class="market-group-card">
              <div class="market-group-title">⚽ Статистика Голов</div>
              <div class="kpi-grid">
                <div class="kpi-card">
                  <span class="kpi-label">Ср. голов ${t1}</span>
                  <span class="kpi-value gold">${stats.team1.stats.avg_goals_scored}</span>
                </div>
                <div class="kpi-card">
                  <span class="kpi-label">Ср. голов ${t2}</span>
                  <span class="kpi-value gold">${stats.team2.stats.avg_goals_scored}</span>
                </div>
                <div class="kpi-card">
                  <span class="kpi-label">ТБ 2.5 % (${t1})</span>
                  <span class="kpi-value green">${stats.team1.stats.over_25_pct}%</span>
                </div>
                <div class="kpi-card">
                  <span class="kpi-label">ТБ 2.5 % (${t2})</span>
                  <span class="kpi-value green">${stats.team2.stats.over_25_pct}%</span>
                </div>
              </div>
            </div>
          ` : ''}
        </div>
      ` : `
        <!-- AI Insights & Preview Menu -->
        <div class="match-insights-container">
          <div class="market-group-card">
            <div class="market-group-title">🧠 Аналитика «Темшик»</div>

            ${insights?.probabilities ? `
              <div class="mc-probs">
                <div class="mc-probs-labels">
                  <span class="prob-home">П1: ${Math.round(insights.probabilities.home * 100)}%</span>
                  <span class="prob-draw">Х: ${Math.round(insights.probabilities.draw * 100)}%</span>
                  <span class="prob-away">П2: ${Math.round(insights.probabilities.away * 100)}%</span>
                </div>
                <div class="h2h-progress-bar mc-probs-bar">
                  <div class="prob-bar-home" style="width:${insights.probabilities.home * 100}%;"></div>
                  <div class="prob-bar-draw" style="width:${insights.probabilities.draw * 100}%;"></div>
                  <div class="prob-bar-away" style="width:${insights.probabilities.away * 100}%;"></div>
                </div>
                <div class="mc-probs-foot">
                  <span>Уверенность: ${Math.round((insights.confidence || 0.6) * 100)}%</span>
                  ${insights.elo ? `<span>Elo: ${insights.elo.rating_t1} vs ${insights.elo.rating_t2}</span>` : ''}
                </div>
              </div>
            ` : ''}

            <!-- Key Factors -->
            <div class="mc-factors">
              <div class="mc-factors-title">💡 Ключевые факторы:</div>
              ${((insights?.key_factors && insights.key_factors.length > 0) ? insights.key_factors : (insights?.insights || [])).map(txt => `
                <div class="insight-card mc-factor">
                  <span>${txt}</span>
                </div>
              `).join('')}
            </div>
          </div>
        </div>
      `}
    `;
  }

  /**
   * Нормализует строку таблицы к одному набору полей: бэкенд отдаёт разные
   * названия колонок в зависимости от источника, поэтому читаем защитно.
   */
  static normalizeStandingsRow(s) {
    const gf = s.goals_scored ?? s.goals_for ?? 0;
    const ga = s.goals_conceded ?? s.goals_against ?? 0;
    return {
      team: s.team_name || s.team || s.player_team || s.name || 'Команда',
      played: s.played ?? s.games ?? 0,
      wins: s.wins ?? s.won ?? 0,
      draws: s.draws ?? s.drawn ?? 0,
      losses: s.losses ?? s.lost ?? 0,
      gf,
      ga,
      diff: gf - ga,
      points: s.points ?? 0
    };
  }

  /**
   * Одна строка списка лидеров: место, логотип клуба, имя, клуб и число справа.
   * Общая для бомбардиров, ассистентов и обладателей награды «Игрок матча» —
   * меняются только иконка и поле со значением.
   */
  static renderLeaderRows(rows, icon, valueOf) {
    return rows.map((row, idx) => `
      <div class="leaders-row">
        <div class="leaders-row-main">
          <span class="leaders-rank" style="color: ${idx < 3 ? 'var(--accent-gold)' : 'var(--text-secondary)'};">#${idx + 1}</span>
          ${renderTeamLogoHtml(row.team_name, 26)}
          <div>
            <div class="leaders-name">${escapeHtml(row.player_name || '')}</div>
            <div class="leaders-team">${escapeHtml(row.team_name || '—')}</div>
          </div>
        </div>
        <div class="leaders-value">
          ${icon} ${valueOf(row)}
        </div>
      </div>
    `).join('');
  }

  static renderTournaments(standings, results, topStats = {}, activeTab = 'standings', form = {}, sort = null, leaderTab = 'scorers') {
    const container = document.getElementById('tournaments-content-container');
    if (!container) return;

    if (activeTab === 'standings') {
      if (!standings || standings.length === 0) {
        container.innerHTML = '<div class="list-empty">Таблица пока пуста.</div>';
        return;
      }

      const sortKey = sort?.key || 'points';
      const sortDir = sort?.dir || 'desc';
      const columns = [
        { key: 'team', label: 'Клуб', title: 'Клуб' },
        { key: 'played', label: 'И', title: 'Игры' },
        { key: 'wins', label: 'В', title: 'Победы' },
        { key: 'draws', label: 'Н', title: 'Ничьи' },
        { key: 'losses', label: 'П', title: 'Поражения' },
        { key: 'gf', label: 'ЗГ', title: 'Забитые голы' },
        { key: 'ga', label: 'ПГ', title: 'Пропущенные голы' },
        { key: 'diff', label: 'Р-Г', title: 'Разница мячей' },
        { key: 'points', label: 'О', title: 'Очки' }
      ];

      // Исходный порядок = позиция в таблице; сохраняем её до пересортировки,
      // чтобы при сортировке по любой колонке было видно реальное место.
      const rows = standings.map((s, idx) => ({ ...UIRenderer.normalizeStandingsRow(s), position: idx + 1 }));

      const dirMul = sortDir === 'asc' ? 1 : -1;
      const sorted = [...rows].sort((a, b) => {
        if (sortKey === 'team') return dirMul * a.team.localeCompare(b.team, 'ru');
        const delta = (a[sortKey] ?? 0) - (b[sortKey] ?? 0);
        if (delta !== 0) return dirMul * delta;
        // Тай-брейк — исходное место, чтобы порядок не «прыгал» между рендерами.
        return a.position - b.position;
      });

      const isDefaultOrder = sortKey === 'points' && sortDir === 'desc';
      const arrow = sortDir === 'asc' ? '▲' : '▼';

      container.innerHTML = `
        <div class="standings-card">
          <table class="standings-table">
            <thead>
              <tr>
                ${columns.map(c => `
                  <th class="sortable${sortKey === c.key ? ' sorted' : ''}" data-sort-key="${c.key}" title="${c.title}">
                    ${c.label}${sortKey === c.key ? `<span class="sort-arrow">${arrow}</span>` : ''}
                  </th>
                `).join('')}
                <th title="Последние 5 матчей">Форма</th>
              </tr>
            </thead>
            <tbody>
              ${sorted.map(r => {
                const diffStr = r.diff > 0 ? `+${r.diff}` : `${r.diff}`;
                const formList = (form && (form[r.team.toLowerCase()] || form[r.team])) || [];
                const formHtml = formList.length
                  ? formList.map(o => `<span class="form-dot form-${String(o).toLowerCase()}">${o}</span>`).join('')
                  : '<span class="standings-dash">—</span>';

                return `
                  <tr>
                    <td class="standings-team-cell">
                      <div class="standings-team">
                        <span class="standings-pos-pill ${isDefaultOrder && r.position <= 3 ? 'top' : 'mid'}">${r.position}</span>
                        ${renderTeamLogoHtml(r.team, 22)}
                        <span class="standings-team-name">${r.team}</span>
                      </div>
                    </td>
                    <td>${r.played}</td>
                    <td>${r.wins}</td>
                    <td>${r.draws}</td>
                    <td>${r.losses}</td>
                    <td>${r.gf}</td>
                    <td>${r.ga}</td>
                    <td class="standings-diff" style="color: ${r.diff > 0 ? 'var(--color-success)' : r.diff < 0 ? 'var(--color-danger)' : 'var(--text-secondary)'};">${diffStr}</td>
                    <td class="standings-points">${r.points}</td>
                    <td><div class="form-strip">${formHtml}</div></td>
                  </tr>
                `;
              }).join('')}
            </tbody>
          </table>
        </div>
      `;
    } else if (activeTab === 'results') {
      if (!results || results.length === 0) {
        container.innerHTML = '<div class="list-empty">Архив результатов пуст.</div>';
        return;
      }
      renderPagedList(container, 'results', results, r => {
        const t1 = r.team1_name || r.player1_team || 'Хозяева';
        const t2 = r.team2_name || r.player2_team || 'Гости';
        return `
          <div class="result-card">
            <div class="result-team-home">
              <span>${t1}</span>
              ${renderTeamLogoHtml(t1, 22)}
            </div>
            <div class="result-score">
              ${r.player1_score ?? 0} : ${r.player2_score ?? 0}
            </div>
            <div class="result-team-away">
              ${renderTeamLogoHtml(t2, 22)}
              <span>${t2}</span>
            </div>
          </div>
        `;
      });
    } else if (activeTab === 'scorers') {
      const leaderViews = {
        scorers: {
          label: '⚽ Бомбардиры',
          icon: '⚽',
          empty: 'Список бомбардиров формируется.',
          rows: topStats?.top_scorers || [],
          valueOf: (r) => r.goals ?? r.total_goals ?? 0
        },
        assists: {
          label: '🎯 Ассистенты',
          icon: '🎯',
          empty: 'Список ассистентов формируется.',
          rows: topStats?.top_assists || [],
          valueOf: (r) => r.assists ?? r.total_assists ?? 0
        },
        mvps: {
          label: '👑 Лидеры MVP',
          icon: '👑',
          empty: 'Наград «Игрок матча» пока нет.',
          rows: topStats?.top_mvps || [],
          valueOf: (r) => r.mvp_count ?? 0
        }
      };

      const activeLeader = leaderViews[leaderTab] ? leaderTab : 'scorers';
      const view = leaderViews[activeLeader];

      const tabsHtml = `
        <div class="mc-tabs">
          ${Object.entries(leaderViews).map(([key, v]) => `
            <button class="mc-subtab-btn${key === activeLeader ? ' active' : ''}" data-leader-tab="${key}">${v.label}</button>
          `).join('')}
        </div>
      `;

      const bodyHtml = view.rows.length === 0
        ? `<div class="list-empty">${view.empty}</div>`
        : `<div class="leaders-card">
             ${UIRenderer.renderLeaderRows(view.rows, view.icon, view.valueOf)}
           </div>`;

      container.innerHTML = tabsHtml + bodyHtml;
    }
  }

  static renderPredictionsHistory(bets, filter = 'all') {
    const container = document.getElementById('history-list-container');
    if (!container) return;

    let filtered = bets || [];
    if (filter !== 'all') {
      if (filter === 'cancelled') {
        filtered = filtered.filter(b => ['cancelled', 'voided', 'void', 'refunded'].includes(b.status));
      } else if (filter === 'refunded') {
        filtered = filtered.filter(b => b.status === 'refunded');
      } else {
        filtered = filtered.filter(b => b.status === filter);
      }
    }

    if (filtered.length === 0) {
      container.innerHTML = `
        <div class="coupons-empty-state history-empty">
          <div class="empty-icon history-empty-icon">📜</div>
          <div class="empty-title history-empty-title">Прогнозов в данной категории не найдено</div>
          <div class="empty-subtitle history-empty-hint">Делайте прогнозы на матчи лиги и отслеживайте их статус здесь</div>
        </div>
      `;
      return;
    }

    const formatAmount = (amt) => {
      if (amt === undefined || amt === null) return '0';
      return Number(amt).toLocaleString('ru-RU');
    };

    const formatDate = (dateStr) => {
      if (!dateStr) return '';
      const m = String(dateStr).match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
      if (m) {
        const [, y, mo, d, hh, mm] = m;
        return `${d}.${mo}.${y} • ${hh}:${mm}`;
      }
      return String(dateStr).substring(0, 16);
    };




    renderPagedList(container, `history:${filter}`, filtered, b => {
      const isWon = b.status === 'won';
      const isLost = b.status === 'lost';
      const isPending = b.status === 'pending';
      const isRefunded = b.status === 'refunded';
      const isCancelled = ['cancelled', 'voided', 'void'].includes(b.status);
      const isCashedOut = !!b.cashout_at;

      let statusKey = 'pending';
      let statusText = 'В ИГРЕ';
      let statusDotColor = 'var(--accent-cyan)';

      if (isCashedOut) {
        statusKey = 'cashout';
        statusText = 'CASHOUT';
        statusDotColor = '#38bdf8';
      } else if (isWon) {
        statusKey = 'won';
        statusText = 'ВЫИГРЫШ';
        statusDotColor = 'var(--color-success)';
      } else if (isLost) {
        statusKey = 'lost';
        statusText = 'ПРОИГРЫШ';
        statusDotColor = 'var(--color-danger)';
      } else if (isRefunded) {
        statusKey = 'refunded';
        statusText = 'ВОЗВРАТ';
        statusDotColor = 'var(--color-warning)';
      } else if (isCancelled) {
        statusKey = 'cancelled';
        statusText = 'ОТМЕНА';
        statusDotColor = 'var(--text-muted)';
      }

      // Summary calculation
      let payoutLabel = 'Выплата';
      let payoutVal = '0';
      let payoutClass = 'val-muted';

      if (isCashedOut) {
        payoutLabel = 'Выплата';
        payoutVal = formatAmount(b.actual_payout);
        payoutClass = 'val-cashout';
      } else if (isWon) {
        payoutLabel = 'Выплата';
        payoutVal = formatAmount(b.actual_payout || b.potential_win);
        payoutClass = 'val-won';
      } else if (isLost) {
        payoutLabel = 'Выплата';
        payoutVal = '0';
        payoutClass = 'val-lost';
      } else if (isRefunded) {
        payoutLabel = 'Выплата';
        payoutVal = formatAmount(b.actual_payout || b.amount);
        payoutClass = 'val-refund';
      } else if (isPending) {
        payoutLabel = b.bet_type === 'express' ? 'Возможный выигрыш' : 'Возможная выплата';
        payoutVal = formatAmount(b.potential_win);
        payoutClass = 'val-pending';
      }

      const isExpress = b.bet_type === 'express';
      const items = b.items || [];
      const showCashout = isPending && !isCashedOut;
      const showRepeat = true;

      return `
        <div class="coupon-card bet-history-card status-${statusKey}" data-bet-id="${b.id}">
          <!-- LEVEL 1: HEADER -->
          <div class="coupon-header">
            <div class="coupon-header-left">
              ${isExpress ? '<span class="coupon-badge-express">⚡ ЭКСПРЕСС</span>' : ''}
              <span class="coupon-id">#${b.id}</span>
              <span class="coupon-meta-dot">•</span>
              <span class="coupon-date">${formatDate(b.created_at)}</span>
              ${b.settled_at ? `
                <span class="coupon-meta-dot">•</span>
                <span class="coupon-settled-date" title="Рассчитано">🏁 ${formatDate(b.settled_at)}</span>
              ` : ''}
            </div>
            <div class="coupon-header-right">
              <span class="coupon-status-badge badge-${statusKey}">
                <span class="status-indicator-dot" style="background-color: ${statusDotColor};"></span>
                ${statusText}
              </span>
            </div>
          </div>

          <!-- LEVEL 2: MATCHES -->
          <div class="coupon-matches-list">
            ${items.map((it, idx) => {
              const acceptedOdd = Number(it.odds_at_placement || it.odd || 1.0).toFixed(2);
              const legWon = it.status === 'won';
              const legLost = it.status === 'lost';
              const legRefund = it.status === 'refunded';
              const legPending = !legWon && !legLost && !legRefund;

              const legClass = legWon ? 'won' : legLost ? 'lost' : legRefund ? 'refunded' : 'pending';
              const legIcon = legWon ? '✓' : legLost ? '✕' : legRefund ? '↩' : '◷';

              const outcomeRaw = it.outcome_type || '';
              const outcomeName = OUTCOME_NAMES[outcomeRaw] || it.selection_name || outcomeRaw.toUpperCase();

              const hasFinishedScore = ['confirmed', 'completed', 'finished'].includes(it.match_status) || (it.player1_score !== null && it.player1_score !== undefined && !isPending);
              const isMatchLive = it.match_status === 'live';

              return `
                <div class="coupon-match-row">
                  <div class="coupon-teams-layout">
                    <!-- Home Team -->
                    <div class="coupon-team home">
                      ${renderTeamLogoWrapperHtml(it.team1_name)}
                      <span class="coupon-team-name" title="${it.team1_name}">${it.team1_name}</span>
                    </div>

                    <!-- Center Score / VS -->
                    <div class="coupon-match-center">
                      ${hasFinishedScore ? `
                        <div class="coupon-score">${it.player1_score ?? 0} : ${it.player2_score ?? 0}</div>
                        <div class="coupon-match-sub">Завершён</div>
                      ` : isMatchLive ? `
                        <div class="coupon-score live">${it.player1_score ?? 0} : ${it.player2_score ?? 0}</div>
                        <div class="coupon-match-sub live">LIVE ${it.live_minute ? it.live_minute + "'" : ''}</div>
                      ` : `
                        <div class="coupon-vs">VS</div>
                        <div class="coupon-match-sub">${it.tournament_type === 'cup' ? escapeHtml(cupStageLabel(it.cup_stage)) : (it.tour ? `Тур ${it.tour}` : 'Матч')}</div>
                      `}
                    </div>

                    <!-- Away Team -->
                    <div class="coupon-team away">
                      <span class="coupon-team-name" title="${it.team2_name}">${it.team2_name}</span>
                      ${renderTeamLogoWrapperHtml(it.team2_name)}
                    </div>
                  </div>

                  <!-- Prediction Subrow -->
                  <div class="coupon-prediction-subrow">
                    <span class="coupon-market-label">${it.market_name || (it.division_id ? `Д${it.division_id}` : 'Исход')}</span>
                    <div class="coupon-prediction-pill ${legClass}">
                      <span class="coupon-pred-outcome">${outcomeName}</span>
                      <span class="coupon-pred-at">@</span>
                      <span class="coupon-pred-odd">${acceptedOdd}</span>
                      <span class="coupon-pred-icon">${legIcon}</span>
                    </div>
                  </div>
                </div>
              `;
            }).join('')}
          </div>

          <!-- LEVEL 3: SUMMARY -->
          <div class="coupon-summary">
            <div class="coupon-summary-col">
              <span class="coupon-summary-label">Ставка</span>
              <span class="coupon-summary-val">${formatAmount(b.amount)} 🪙</span>
            </div>
            <div class="coupon-summary-col">
              <span class="coupon-summary-label">${isExpress ? 'Общий коэф.' : 'Коэффициент'}</span>
              <span class="coupon-summary-val gold">${Number(b.total_odd || 1.0).toFixed(2)}</span>
            </div>
            <div class="coupon-summary-col">
              <span class="coupon-summary-label">${payoutLabel}</span>
              <span class="coupon-summary-val ${payoutClass}">${payoutVal} 🪙</span>
            </div>
          </div>

          <!-- LEVEL 4: ACTIONS -->
          ${showCashout || showRepeat ? `
            <div class="coupon-actions">
              ${showCashout ? `
                <button class="btn-cashout coupon-btn-cashout" data-bet-id="${b.id}">
                  💰 Cashout
                </button>
              ` : ''}
              ${showRepeat ? `
                <button class="btn-repeat-bet coupon-btn-repeat" data-bet-id="${b.id}">
                  ↻ Повторить прогноз
                </button>
              ` : ''}
            </div>
          ` : ''}
        </div>
      `;
    });
  }

  static showOddsChangedModal(oldOdd, newOdd, onAccept, onReject) {
    const modal = document.getElementById('odds-changed-modal');
    const desc = document.getElementById('odds-changed-modal-desc');
    const acceptBtn = document.getElementById('btn-accept-odds-change');
    const rejectBtn = document.getElementById('btn-reject-odds-change');

    if (!modal) return;

    if (desc) {
      desc.innerHTML = `Коэффициент одного из исходов изменился: <b class="odds-old">${Number(oldOdd).toFixed(2)}</b> → <b class="odds-new">${Number(newOdd).toFixed(2)}</b>.<br>Принять новые условия?`;
    }

    const cleanup = () => {
      modal.classList.remove('active');
    };

    const handleAccept = (e) => {
      e.stopPropagation();
      cleanup();
      if (typeof onAccept === 'function') onAccept();
    };

    const handleReject = (e) => {
      e.stopPropagation();
      cleanup();
      if (typeof onReject === 'function') onReject();
    };

    if (acceptBtn) {
      const newAccept = acceptBtn.cloneNode(true);
      acceptBtn.parentNode.replaceChild(newAccept, acceptBtn);
      newAccept.addEventListener('click', handleAccept);
    }
    if (rejectBtn) {
      const newReject = rejectBtn.cloneNode(true);
      rejectBtn.parentNode.replaceChild(newReject, rejectBtn);
      newReject.addEventListener('click', handleReject);
    }

    modal.classList.add('active');
  }

  static renderSavedCoupons(savedCoupons) {
    const container = document.getElementById('saved-coupons-container');
    if (!container) return;

    if (!savedCoupons || savedCoupons.length === 0) {
      container.innerHTML = '';
      return;
    }

    container.innerHTML = `
      <div class="saved-title">
        💾 Сохраненные Черновики (${savedCoupons.length})
      </div>
      ${savedCoupons.map(sc => `
        <div class="saved-coupon-card">
          <div>
            <div class="saved-name">${escapeHtml(sc.name || 'Купон')}</div>
            <div class="saved-meta">
              ${sc.selections?.length || 0} событий | Кэф: ${(sc.total_odd || 1.0).toFixed(2)}
            </div>
          </div>
          <div class="saved-actions">
            <button class="btn-restore-coupon" data-saved-id="${sc.id}">Загрузить</button>
            <button class="btn-delete-saved-coupon saved-delete" data-saved-id="${sc.id}">✕</button>
          </div>
        </div>
      `).join('')}
    `;
  }

  static renderProfile(user, progression, stats, achievements) {
    const cardEl = document.getElementById('profile-card-container');
    if (cardEl && user) {
      const uName = user.username ? `@${user.username}` : (user.first_name || 'Каппер');
      const tgUser = tgBridge.getUser();
      const photoUrl = user.photo_url || tgUser?.photo_url || null;
      // Первая буква имени из Telegram вставляется и в onerror-атрибут: кавычка
      // или «<» в ней ломали разметку, поэтому только буквы и цифры.
      const rawInitial = (user.username || user.first_name || 'K').replace('@', '').charAt(0).toUpperCase();
      const initial = /^[\p{L}\p{N}]$/u.test(rawInitial) ? rawInitial : 'K';

      const avatarHtml = photoUrl 
        ? `<img src="${photoUrl}" alt="Avatar" class="user-profile-avatar-img" onerror="this.outerHTML='<div class=\\'user-profile-avatar-fallback\\'>${initial}</div>'" />`
        : `<div class="user-profile-avatar-fallback">${initial}</div>`;

      cardEl.innerHTML = `
        <div class="profile-head">
          <div class="user-profile-avatar-container">
            ${avatarHtml}
          </div>
          <div>
            <div class="profile-name">
              ${escapeHtml(uName)}
            </div>
            <div class="profile-level">
              ${progression?.equipped_title || 'Каппер Лиги'} • Уровень ${progression?.level || 1}
            </div>
          </div>
        </div>
      `;
    }

    // KPI Metrics
    if (stats) {
      const roiEl = document.getElementById('kpi-roi');
      if (roiEl) {
        roiEl.textContent = `${stats.roi_pct > 0 ? '+' : ''}${stats.roi_pct}%`;
        roiEl.className = `kpi-value ${stats.roi_pct >= 0 ? 'green' : 'red'}`;
      }
      const wrEl = document.getElementById('kpi-winrate');
      if (wrEl) wrEl.textContent = `${stats.win_rate_pct}%`;
      const avgEl = document.getElementById('kpi-avg-odds');
      if (avgEl) avgEl.textContent = (stats.average_odds || 1.0).toFixed(2);
      const bestEl = document.getElementById('kpi-best-win');
      if (bestEl) bestEl.textContent = `${this.formatNumber(stats.best_win)} 🪙`;
    }

    // Achievements Grid
    const achEl = document.getElementById('achievements-grid-container');
    const achCountEl = document.getElementById('achievements-count-label');
    if (achEl && achievements) {
      const unlocked = achievements.filter(a => a.is_unlocked).length;
      if (achCountEl) achCountEl.textContent = `${unlocked}/${achievements.length}`;

      // The catalog columns are `name` / `badge_icon` — не `title` / `icon`.
      // Награда показывается на каждой карточке, в том числе на закрытой:
      // это и есть ответ на вопрос «сколько дадут за квест».
      renderPagedList(achEl, 'achievements', achievements, a => {
        const coins = Number(a.reward_coins) || 0;
        const xp = Number(a.reward_xp) || 0;
        const canClaim = a.is_unlocked && !a.is_claimed;
        return `
        <div class="achievement-card ${a.is_unlocked ? 'unlocked' : 'locked'}" data-ach-id="${a.id}">
          <div class="ach-icon ach-icon-big">${a.badge_icon || '🏆'}</div>
          <div class="ach-text">
            <div class="ach-title ach-title-text">${a.name || 'Достижение'}</div>
            <div class="ach-desc ach-about">${a.description || ''}</div>
          </div>
          <div class="ach-reward ach-reward-line">
            +${this.formatNumber(coins)} 🪙${xp ? ` <span class="ach-xp">· +${xp} XP</span>` : ''}
          </div>
          ${canClaim ? `
            <button class="btn-claim-achievement ach-claim-btn" data-claim-ach-id="${a.id}">Забрать</button>
          ` : a.is_unlocked ? `
            <div class="ach-status ach-got">Получено</div>
          ` : ''}
        </div>
      `;
      });
    }
  }

  static renderSlipDrawer(slip, stakeAmount) {
    const count = slip.length;
    const mode = store.getSlipMode();
    const isExpress = mode === 'express';
    const batchSingles = store.isBatchSingles();
    const totalOdd = store.getTotalOdd();
    const totalStake = store.getTotalStake();
    const potentialWin = store.getPotentialWin();
    const { min_bet, max_payout, max_open_exposure, max_open_bets, open_bets } = store.getBetLimits();
    const balance = Math.floor(store.state.user?.balance || 0);
    const fmt = (n) => this.formatNumber(n);
    const setText = (id, text) => {
      const el = document.getElementById(id);
      if (el && el.textContent !== text) el.textContent = text;
    };

    document.body.classList.toggle('has-coupon', count > 0);

    // ─── Floating bar ───
    const bar = document.getElementById('betbar');
    if (bar) {
      const prevCount = parseInt(bar.dataset.count || '0', 10);
      bar.dataset.count = String(count);
      const sheetOpen = document.getElementById('coupon-sheet')?.classList.contains('open');
      const visible = count > 0 && !sheetOpen;
      bar.classList.toggle('visible', visible);
      bar.setAttribute('aria-hidden', visible ? 'false' : 'true');
      if (count > 0 && prevCount > 0 && count !== prevCount) {
        bar.classList.remove('bump');
        void bar.offsetWidth; // restart the animation
        bar.classList.add('bump');
      }
      setText('betbar-count', String(count));
      setText('betbar-mode', batchSingles ? 'Ординары' : (isExpress ? 'Экспресс' : 'Ординар'));
      setText('betbar-odd', batchSingles ? `×${count}` : totalOdd.toFixed(2));
    }

    // ─── Segmented control ───
    const seg = document.getElementById('coupon-seg');
    if (seg) {
      seg.dataset.mode = mode;
      seg.querySelectorAll('.coupon-seg-btn').forEach(btn => {
        const active = btn.dataset.slipMode === mode;
        btn.classList.toggle('active', active);
        btn.setAttribute('aria-selected', active ? 'true' : 'false');
        if (btn.dataset.slipMode === 'express') btn.disabled = count < 2;
      });
    }
    setText('coupon-seg-hint', count === 1 ? 'Добавьте ещё 1 событие для экспресса' : '');

    // ─── Event cards: rebuilt only when the structure changes ───
    const itemsEl = document.getElementById('slip-items-container');
    if (itemsEl) {
      const structKey = `${mode}|${batchSingles}|` + slip.map(s => `${s.match_id}:${s.outcome}:${s.odd}`).join(',');
      if (itemsEl.dataset.key !== structKey) {
        itemsEl.dataset.key = structKey;
        itemsEl.classList.toggle('express', isExpress);
        itemsEl.innerHTML = count === 0
          ? '<div class="slip-empty">Выберите исходы матчей для добавления в купон</div>'
          : slip.map((s, i) => {
              const card = this._renderSlipCard(s, batchSingles);
              const link = isExpress && i < count - 1
                ? '<div class="coupon-chain-link"><span class="coupon-chain-node">×</span></div>'
                : '';
              return card + link;
            }).join('') + (isExpress
              ? `<div class="coupon-chain-total"><span>Экспресс из ${count} событий</span><b>${totalOdd.toFixed(2)}</b></div>`
              : '');
      }

      // Live values that change on every keystroke
      if (batchSingles) {
        slip.forEach(s => {
          const stake = store.getSingleStake(s.match_id);
          const input = itemsEl.querySelector(`.coupon-single-stake[data-match-id="${s.match_id}"]`);
          if (input && document.activeElement !== input && input.value !== String(stake)) {
            input.value = String(stake);
          }
          const payoutEl = itemsEl.querySelector(`[data-payout-for="${s.match_id}"]`);
          if (payoutEl) payoutEl.textContent = `${fmt(Math.round(stake * s.odd))} 🪙`;
        });
      }
    }

    // ─── Stake field ───
    // With differing per-event stakes the field stays empty: typing sets them all.
    const commonStake = store.getCommonStake();
    setText('coupon-stake-label', !batchSingles ? 'Сумма ставки'
      : (commonStake === null ? 'Ставка на все события' : 'Ставка на каждое событие'));
    const stakeInput = document.getElementById('stake-input');
    if (stakeInput) {
      stakeInput.placeholder = commonStake === null ? 'разные' : '';
      const want = commonStake === null ? '' : String(commonStake);
      if (document.activeElement !== stakeInput && stakeInput.value !== want) stakeInput.value = want;
    }
    setText('coupon-stake-balance', `Баланс ${fmt(balance)}`);

    // ─── Summary ───
    setText('coupon-summary-odd-label', batchSingles ? 'Ординаров' : (isExpress ? 'Общий кэф' : 'Коэффициент'));
    setText('coupon-summary-odd', batchSingles ? String(count) : totalOdd.toFixed(2));
    setText('coupon-summary-stake', `${fmt(totalStake)} 🪙`);
    setText('slip-forecast-val', `${fmt(potentialWin)} 🪙`);
    setText('coupon-summary-max-label', `Макс. ставка (выигрыш до ${fmt(max_payout)})`);
    setText('coupon-summary-max', `${fmt(store.getMaxStakeByPayout())} 🪙`);

    // Счётчик слотов показываем всегда, а не только при отказе: уже открытые
    // купоны занимают слоты, и без счётчика непонятно, куда они делись.
    const safeOpenBets = Number.isFinite(open_bets) ? open_bets : (store.state.user?.bet_limits?.open_bets ?? 0);
    const safeMaxOpenBets = Number.isFinite(max_open_bets) ? max_open_bets : (store.state.user?.bet_limits?.max_open_bets ?? 12);
    const freeSlots = typeof store.getRemainingBetSlots === 'function'
      ? store.getRemainingBetSlots()
      : Math.max(0, safeMaxOpenBets - safeOpenBets);
    setText('coupon-summary-slots', `${safeOpenBets} из ${safeMaxOpenBets}`);
    document.getElementById('coupon-slots-row')?.classList.toggle('is-full', freeSlots === 0);

    // ─── Validation ───
    // `warning` гасит кнопку, `notice` — просто предупреждает: пачку ординаров,
    // которая не влезает в остаток слотов, сервер примет частично, и мешать
    // отправке не нужно — надо лишь честно сказать, сколько уйдёт.
    let warning = '';
    let notice = '';
    if (count > 0) {
      const stakes = batchSingles ? slip.map(s => store.getSingleStake(s.match_id)) : [stakeAmount];
      const odds = batchSingles ? slip.map(s => s.odd) : [totalOdd];
      const remaining = store.getRemainingExposure();
      // Купонов на ставку: экспресс — один, пачка ординаров — по одному на событие.
      const neededSlots = batchSingles ? count : 1;
      if (freeSlots === 0) {
        warning = `Открыто ${open_bets} из ${max_open_bets} купонов — дождитесь расчёта`;
      } else if (stakes.some(v => v < min_bet)) warning = `Минимальная ставка — ${fmt(min_bet)} 🪙`;
      else if (totalStake > balance) warning = `Недостаточно средств: нужно ${fmt(totalStake)} 🪙, на балансе ${fmt(balance)} 🪙`;
      else if (stakes.some((v, i) => Math.round(v * odds[i]) > max_payout)) {
        warning = `Выигрыш с одной ставки — не больше ${fmt(max_payout)} 🪙. Макс. ставка при этом кэфе: ${fmt(store.getMaxStakeByPayout())} 🪙`;
      } else if (potentialWin > remaining) {
        warning = remaining < Math.round(min_bet * Math.min(...odds))
          ? `Лимит открытых ставок (${fmt(max_open_exposure)} 🪙 выигрыша) исчерпан — дождитесь расчёта`
          : `Лимит открытых ставок: осталось ${fmt(remaining)} 🪙 выигрыша. Макс. ставка: ${fmt(store.getMaxStake())} 🪙`;
      }
      if (!warning && neededSlots > freeSlots) {
        notice = `Свободно слотов: ${freeSlots} — примем ${freeSlots} ${this._pluralOrdinar(freeSlots)} из ${count}`;
      }
    }
    setText('coupon-warning', warning || notice);
    document.getElementById('coupon-warning')?.classList.toggle('is-notice', !warning && !!notice);

    // ─── CTA ───
    const submitBtn = document.getElementById('btn-submit-prediction');
    if (submitBtn && !submitBtn.classList.contains('loading')) {
      let main = 'Выберите событие';
      let sub = '';
      if (batchSingles) {
        main = `Поставить ${count} ${this._pluralOrdinar(count)}`;
        sub = `Всего: ${fmt(totalStake)} 🪙 → Выигрыш до ${fmt(potentialWin)} 🪙`;
      } else if (isExpress) {
        main = `Поставить экспресс ${fmt(stakeAmount)} 🪙`;
        sub = `→ Выигрыш ${fmt(potentialWin)} 🪙`;
      } else if (count === 1) {
        main = `Поставить ${fmt(stakeAmount)} 🪙`;
        sub = `→ Выигрыш ${fmt(potentialWin)} 🪙`;
      }
      setText('coupon-cta-main', main);
      setText('coupon-cta-sub', sub);
      submitBtn.disabled = count === 0 || !!warning;
    }
  }

  static _pluralOrdinar(n) {
    const mod10 = n % 10;
    const mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return 'ординар';
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return 'ординара';
    return 'ординаров';
  }

  static _renderSlipCard(s, withStake) {
    // Line tiles carry no name, so the store falls back to the upper-cased key ("P1").
    const hasOwnName = s.selection_name && s.selection_name !== String(s.outcome).toUpperCase();
    const outcome = hasOwnName ? s.selection_name : (OUTCOME_NAMES[s.outcome] || s.selection_name || s.outcome);
    const market = s.market_name || marketNameForOutcome(s.outcome);
    const stakeRow = withStake ? `
      <div class="slip-card-stake">
        <label class="coupon-mini-field">
          <span>🪙</span>
          <input type="number" inputmode="numeric" min="0" class="coupon-single-stake"
                 data-match-id="${s.match_id}" value="${store.getSingleStake(s.match_id)}" aria-label="Ставка на событие">
        </label>
        <div class="slip-card-win">Выигрыш<b data-payout-for="${s.match_id}">0 🪙</b></div>
      </div>` : '';
    return `
      <div class="slip-card" data-match-id="${s.match_id}">
        <div class="slip-card-top">
          <div class="slip-card-logos">
            ${renderTeamLogoHtml(s.team1_name, 22)}
            ${renderTeamLogoHtml(s.team2_name, 22)}
          </div>
          <div class="slip-card-match">
            <div class="slip-card-teams">${escapeHtml(s.team1_name)} — ${escapeHtml(s.team2_name)}</div>
            <div class="slip-card-meta">${escapeHtml(s.meta || `Тур ${s.tour || 1}`)} · ${escapeHtml(market)}</div>
          </div>
          <button class="slip-card-remove btn-remove-slip-item" data-match-id="${s.match_id}" aria-label="Убрать событие">✕</button>
        </div>
        <div class="slip-card-pick">
          <span class="slip-card-outcome">${escapeHtml(outcome)}</span>
          <span class="coupon-odd-pill">${Number(s.odd).toFixed(2)}</span>
        </div>
        ${stakeRow}
      </div>`;
  }

  static renderMatchMarketsModal(matchId, markets, matchTitle) {
    const titleEl = document.getElementById('modal-match-title');
    const listEl = document.getElementById('modal-markets-list');
    if (titleEl && matchTitle) titleEl.textContent = matchTitle;

    if (!listEl) return;

    if (!markets || markets.length === 0) {
      listEl.innerHTML = '<div class="modal-loading">Котировки формируются...</div>';
      return;
    }

    const getMarketIcon = (key) => {
      if (key === '1x2') return '⚡ ';
      if (key === 'double_chance') return '🔄 ';
      if (key === 'total_goals') return '⚽ ';
      if (key === 'btts') return '🥅 ';
      if (key === 'handicap') return '↔️ ';
      if (key && key.startsWith('individual_total')) return '🎯 ';
      return '📋 ';
    };

    const getCols = (m) => {
      if (m.market_key === '1x2' || m.market_key === 'double_chance') return 3;
      return 2;
    };

    const marketsHtml = markets.map(m => `
      <div class="mm-group">
        <div class="mm-group-title">
          ${getMarketIcon(m.market_key)}${escapeHtml(m.market_name || m.name || 'Рынок')}
        </div>
        <div class="mm-grid" style="grid-template-columns: repeat(${getCols(m)}, 1fr);">
          ${(m.selections || []).map(sel => {
            const isSel = store.isSelectionActive(matchId, sel.selection_key);
            const sName = sel.selection_name || sel.name || sel.selection_key;
            const sOdd = sel.current_odd || sel.odds_value || 1.90;
            return `
              <div class="odd-btn ${isSel ? 'selected' : ''}" 
                   data-match-id="${matchId}" 
                   data-outcome="${sel.selection_key}" 
                   data-odd="${sOdd}"
                   data-market-id="${m.id}"
                   data-selection-id="${sel.id}"
                   data-market-name="${escapeHtml(m.market_name || m.name || '')}"
                   data-selection-name="${escapeHtml(sName)}">
                <span class="odd-label">${escapeHtml(sName)}</span>
                <span class="odd-val">${Number(sOdd).toFixed(2)}</span>
              </div>
            `;
          }).join('')}
        </div>
      </div>
    `).join('');

    const actionHtml = `
      <div class="mm-footer">
        <button class="btn-match-action btn-open-match-center mm-open-center" data-match-id="${matchId}">
          <span>📊</span> Открыть полную аналитику & H2H →
        </button>
      </div>
    `;

    listEl.innerHTML = marketsHtml + actionHtml;
  }

  static renderLeaderboardModal(leaderboard, myRank) {
    const podiumEl = document.getElementById('leaderboard-podium');
    const listEl = document.getElementById('leaderboard-list');

    if (!leaderboard || leaderboard.length === 0) {
      if (listEl) listEl.innerHTML = '<div class="modal-loading">Зал славы формируется...</div>';
      return;
    }

    const top3 = leaderboard.slice(0, 3);
    const rest = leaderboard.slice(3);

    if (podiumEl) {
      podiumEl.innerHTML = top3.map((p, idx) => `
        <div class="podium-col rank-${idx + 1} lb-podium-col">
          <div class="lb-medal">${idx === 0 ? '🥇' : idx === 1 ? '🥈' : '🥉'}</div>
          <div class="lb-podium-name">${escapeHtml(p.username || 'Игрок')}</div>
          <div class="lb-podium-balance">${this.formatNumber(p.balance)} 🪙</div>
        </div>
      `).join('');
    }

    if (listEl) {
      listEl.innerHTML = rest.map((p, idx) => `
        <div class="lb-row">
          <div class="lb-row-main">
            <span class="lb-rank">#${idx + 4}</span>
            <span class="lb-name">${escapeHtml(p.username || 'Игрок')}</span>
          </div>
          <div class="lb-balance">${this.formatNumber(p.balance)} 🪙</div>
        </div>
      `).join('');
    }
  }

  // ─── Sports Intelligence ──────────────────────────────────────────────
  // LIVE-центр удалён из мини-приложения, renderLiveCenter больше не нужен.

  static renderHotMatches(hotMatches) {
    const el = document.getElementById('hot-matches-container');
    if (!el) return;

    if (!hotMatches || hotMatches.length === 0) {
      el.innerHTML = '';
      return;
    }

    el.innerHTML = `
      <div class="feed-section">
        <div class="feed-head">
          <span class="feed-title">
            🔥 Топ Горячих Матчей
          </span>
          <span class="feed-tag-gold">Scoring Engine</span>
        </div>
        <div class="hot-scroll">
          ${hotMatches.slice(0, 5).map(m => `
            <div class="hot-card">
              <div class="hot-meta">
                <span>Тур ${m.round_number || 1}</span>
                <span class="hot-score">🔥 ${m.hot_score} pts</span>
              </div>
              <div class="hot-teams">
                ${m.player1_team} — ${m.player2_team}
              </div>
              <div class="hot-foot">
                <span class="hot-reason">${m.reasons ? m.reasons[0] : 'Высокий интерес'}</span>
              </div>
            </div>
          `).join('')}
        </div>
      </div>
    `;
  }

  static renderOddsMovers(oddsMovers) {
    const el = document.getElementById('odds-movers-container');
    if (!el) return;

    if (!oddsMovers || oddsMovers.length === 0) {
      el.innerHTML = '';
      return;
    }

    el.innerHTML = `
      <div class="feed-section">
        <div class="feed-head">
          <span class="feed-title">
            📈 Движение Коэффициентов
          </span>
          <span class="feed-tag-cyan">Live Volatility</span>
        </div>
        <div class="movers-scroll">
          ${oddsMovers.slice(0, 6).map(mov => {
            const isDrop = mov.direction === 'down';
            const arrow = isDrop ? '▼' : '▲';
            const color = isDrop ? 'var(--color-success)' : 'var(--color-danger)';
            return `
              <div class="mover-card">
                <div class="mover-match">${mov.player1_team} - ${mov.player2_team}</div>
                <div class="mover-selection">${mov.selection_name || mov.outcome_type || 'Исход'}</div>
                <div class="mover-odds">
                  <span class="odds-old">${mov.previous_odds ? mov.previous_odds.toFixed(2) : ''}</span>
                  <span class="mover-current">${mov.current_odds.toFixed(2)}</span>
                  <span style="color: ${color};">${arrow} ${Math.abs(mov.pct_change).toFixed(1)}%</span>
                </div>
              </div>
            `;
          }).join('')}
        </div>
      </div>
    `;
  }

  /** Ники тренеров под парой клубов: в том же порядке, через то же тире. */
  static _recPlayerTags(rec) {
    const tag = (u) => {
      const v = (u || '').trim();
      return v ? (v.startsWith('@') ? v : `@${v}`) : '';
    };
    const t1 = tag(rec.player1_username);
    const t2 = tag(rec.player2_username);
    if (!t1 && !t2) return '';
    return `<div class="rec-tags">${escapeHtml(t1 || '—')} — ${escapeHtml(t2 || '—')}</div>`;
  }

  static renderRecommendations(recommendations, searchQuery = '') {
    const el = document.getElementById('recommendations-container');
    if (!el) return;

    if (!recommendations || recommendations.length === 0) {
      el.innerHTML = '';
      return;
    }

    let list = recommendations;
    if (searchQuery && searchQuery.trim()) {
      const q = searchQuery.toLowerCase().trim();
      list = list.filter(rec =>
        (rec.player1_team || '').toLowerCase().includes(q) ||
        (rec.player2_team || '').toLowerCase().includes(q) ||
        (rec.player1_username || '').toLowerCase().includes(q.replace(/^@/, '')) ||
        (rec.player2_username || '').toLowerCase().includes(q.replace(/^@/, ''))
      );
      if (list.length === 0) {
        el.innerHTML = '';
        return;
      }
    }

    el.innerHTML = `
      <div class="feed-section">
        <div class="feed-head">
          <span class="feed-title">
            💡 Рекомендации для вас
          </span>
          <span class="feed-tag-gold">Персонально</span>
        </div>
        <div class="rec-list">
          ${list.slice(0, 3).map(rec => `
            <div class="rec-card">
              <div>
                <div class="rec-teams">${escapeHtml(rec.player1_team)} — ${escapeHtml(rec.player2_team)}</div>
                ${UIRenderer._recPlayerTags(rec)}
                <div class="rec-reason">${rec.reason || 'Высокий интерес'}</div>
              </div>
              <button class="btn-open-match-center rec-open-btn" data-match-id="${rec.match_id}">
                Аналитика →
              </button>
            </div>
          `).join('')}
        </div>
      </div>
    `;
  }

  // ==========================================================================
  // My Club («Мой Клуб») — личный кабинет игрока
  // ==========================================================================

  /** Пустое состояние для игрока, не заявленного ни за один клуб. */
  static renderMyClubEmptyState() {
    return `
      <div class="club-empty-state">
        <div class="club-empty-icon">🛡</div>
        <div class="club-empty-title">Ожидание назначения клуба</div>
        <div class="club-empty-text">
          Личный кабинет откроется, как только администратор закрепит за вами клуб и распределит в дивизион.
        </div>
        <ol class="club-empty-steps">
          <li>Откройте бота «Логово Фифарей» и отправьте <b>/start</b>.</li>
          <li>Дождитесь, пока администратор назначит вам клуб (или свяжитесь с организатором турнира).</li>
          <li>После назначения администратором личный кабинет станет доступен автоматически.</li>
        </ol>
      </div>
    `;
  }

  /** Ошибка загрузки кабинета клуба с кнопкой повтора. */
  static renderMyClubError(errorMessage, onRetry = null) {
    const heroEl = document.getElementById('my-club-hero-container');
    if (!heroEl) return;
    heroEl.innerHTML = `
      <div class="club-hero club-error">
        <div class="club-error-icon">⚠️</div>
        <div class="club-error-title">Не удалось загрузить данные клуба</div>
        <div class="club-error-text">${escapeHtml(errorMessage || 'Сервер временно недоступен или перезагружается')}</div>
        <button id="btn-retry-my-club" class="btn-club-secondary club-retry-btn">🔄 Повторить</button>
      </div>
    `;
    const btn = document.getElementById('btn-retry-my-club');
    if (btn && onRetry) {
      btn.addEventListener('click', onRetry);
    }
  }

  /** Баннер клуба + дисциплина. */
  static renderMyClubView(overview) {
    this.updateNavClubIcon(overview);
    const heroEl = document.getElementById('my-club-hero-container');
    if (!heroEl) return;

    if (!overview) {
      heroEl.innerHTML = `
        <div class="club-hero club-hero-skeleton">
          <div class="club-loading">Загрузка клуба...</div>
        </div>
      `;
      return;
    }

    const subtabs = document.getElementById('my-club-subtabs');
    if (!overview.registered) {
      heroEl.innerHTML = UIRenderer.renderMyClubEmptyState();
      if (subtabs) subtabs.style.display = 'none';
      ['my-club-matches-container', 'my-club-squad-container', 'my-club-history-container']
        .forEach(id => {
          const el = document.getElementById(id);
          if (el) el.innerHTML = '';
        });
      return;
    }
    if (subtabs) subtabs.style.display = '';

    const club = overview.club || {};
    const t = overview.tournament || {};
    const discipline = overview.discipline || { warns: 0, limit: 0 };

    const teamName = club.team_name || 'Мой клуб';
    const divLabel = club.division_name || (club.division_id ? `Дивизион ${club.division_id}` : 'Лига');
    const place = t.position ? `${t.position}` : '—';
    const diff = (t.goal_diff || 0) > 0 ? `+${t.goal_diff}` : `${t.goal_diff || 0}`;

    const warns = Number(discipline.warns || 0);
    const limit = Number(discipline.limit || 0);
    const warnLevel = limit && warns >= limit ? 'danger' : (warns > 0 ? 'warning' : 'ok');

    const formHtml = (t.form || []).map(r => {
      const cls = r === 'W' ? 'win' : (r === 'D' ? 'draw' : 'loss');
      const label = r === 'W' ? 'В' : (r === 'D' ? 'Н' : 'П');
      return `<span class="club-form-dot ${cls}">${label}</span>`;
    }).join('');

    heroEl.innerHTML = `
      <div class="club-hero">
        <div class="club-hero-top">
          ${renderTeamLogoWrapperHtml(teamName, 'club-hero-crest')}
          <div class="club-hero-id">
            <div class="club-hero-name">${escapeHtml(teamName)}</div>
            <div class="club-hero-division">${escapeHtml(divLabel)}</div>
          </div>
          <div class="club-place-badge">
            <span class="club-place-value">${escapeHtml(place)}</span>
            <span class="club-place-label">место${t.total_teams ? ` / ${t.total_teams}` : ''}</span>
          </div>
        </div>

        <div class="club-kpi-row">
          <div class="club-kpi"><span class="club-kpi-label">Очки</span><span class="club-kpi-value gold">${t.points || 0}</span></div>
          <div class="club-kpi"><span class="club-kpi-label">Игр</span><span class="club-kpi-value">${t.played || 0}</span></div>
          <div class="club-kpi"><span class="club-kpi-label">В / Н / П</span><span class="club-kpi-value">${t.wins || 0} / ${t.draws || 0} / ${t.losses || 0}</span></div>
          <div class="club-kpi"><span class="club-kpi-label">Мячи</span><span class="club-kpi-value">${t.goals_scored || 0}:${t.goals_conceded || 0} <span class="club-kpi-sub">(${diff})</span></span></div>
        </div>

        ${formHtml ? `
          <div class="club-form-row">
            <span class="club-form-caption">Форма</span>
            <div class="club-form-dots">${formHtml}</div>
          </div>
        ` : ''}

        <div class="club-discipline ${warnLevel}">
          <span class="club-discipline-label">Дисциплина</span>
          <span class="club-discipline-value">${warns} / ${limit} ⚠️</span>
        </div>
      </div>
    `;
  }

  /** Бейдж состояния согласования времени для карточки матча. */
  static _clubMatchBadge(match) {
    if (match.status === 'confirmed') {
      return { cls: 'finished', text: 'Завершён' };
    }
    if (match.status === 'disputed') {
      return { cls: 'disputed', text: 'Спорный' };
    }
    if (match.time_status === 'accepted' && match.proposed_time) {
      return { cls: 'accepted', text: `Согласовано время: ${escapeHtml(match.proposed_time)}` };
    }
    if (match.time_status === 'proposed') {
      return match.proposed_by_me
        ? { cls: 'proposed', text: `Вы предложили: ${escapeHtml(match.proposed_time || '—')}` }
        : { cls: 'incoming', text: `Соперник предлагает: ${escapeHtml(match.proposed_time || '—')}` };
    }
    if (match.status === 'reported') {
      return { cls: 'reported', text: 'Результат на проверке' };
    }
    return { cls: 'pending', text: 'Ожидает игры' };
  }

  static _clubMatchCard(match, { showActions = true } = {}) {
    const badge = UIRenderer._clubMatchBadge(match);
    const sideLabel = match.is_home ? 'Дома' : 'В гостях';
    const opponentTeam = match.opponent_team || 'Соперник';
    const opponentUser = match.opponent_user ? `@${match.opponent_user}` : 'ник не указан';
    const scoreHtml = (match.my_score !== null && match.my_score !== undefined &&
                       match.opp_score !== null && match.opp_score !== undefined)
      ? `<div class="club-match-score">${match.my_score} : ${match.opp_score}</div>`
      : '';

    const canAccept = showActions && match.time_status === 'proposed' && !match.proposed_by_me;
    const actionsHtml = showActions ? `
      <div class="club-match-actions">
        <button class="btn-club-secondary btn-propose-time"
                data-match-id="${match.id}"
                data-opponent="${escapeHtml(opponentTeam)}"
                data-current-time="${escapeHtml(match.proposed_time || '')}">
          🗓 Предложить время
        </button>
        ${canAccept ? `
          <button class="btn-club-primary btn-accept-time" data-match-id="${match.id}">
            ✅ Подтвердить
          </button>
        ` : ''}
      </div>
    ` : '';

    const protocolBtnHtml = !showActions ? `
      <div class="club-match-actions club-protocol-actions">
        <button class="btn-club-secondary btn-view-match-protocol club-protocol-btn" data-match-id="${match.id}">
          📋 Протокол & Скриншот
        </button>
      </div>
    ` : '';

    return `
      <div class="club-match-card ${!showActions ? 'clickable' : ''}" data-match-id="${match.id}">
        <div class="club-match-head">
          <span class="club-match-round">${escapeHtml(matchRoundLabel(match))} · ${sideLabel}</span>
          <span class="club-badge ${badge.cls}">${badge.text}</span>
        </div>
        <div class="club-match-body">
          ${renderTeamLogoWrapperHtml(opponentTeam, 'club-match-crest')}
          <div class="club-match-opponent">
            <div class="club-match-team">${escapeHtml(opponentTeam)}</div>
            <div class="club-match-user">${escapeHtml(opponentUser)}</div>
          </div>
          ${scoreHtml}
        </div>
        ${match.deadline ? `<div class="club-match-deadline">⏳ Дедлайн тура: ${escapeHtml(match.deadline)}</div>` : ''}
        ${actionsHtml}
        ${protocolBtnHtml}
      </div>
    `;
  }

  /** Активные матчи игрока. */
  static renderMyClubMatches(matches, isLoading = false) {
    const el = document.getElementById('my-club-matches-container');
    if (!el) return;

    if (isLoading && (!matches || matches.length === 0)) {
      el.innerHTML = `<div class="club-placeholder">Загрузка матчей...</div>`;
      return;
    }

    if (!matches || matches.length === 0) {
      el.innerHTML = `
        <div class="club-placeholder">
          Активных матчей нет — ждём открытия следующего тура. ⚽
        </div>
      `;
      return;
    }

    el.innerHTML = matches.map(m => UIRenderer._clubMatchCard(m)).join('');
  }

  /** Последние сыгранные матчи клуба. */
  static renderMyClubHistory(recent, isLoading = false) {
    const el = document.getElementById('my-club-history-container');
    if (!el) return;

    if (isLoading && (!recent || recent.length === 0)) {
      el.innerHTML = `<div class="club-placeholder">Загрузка истории...</div>`;
      return;
    }

    if (!recent || recent.length === 0) {
      el.innerHTML = `<div class="club-placeholder">Сыгранных матчей пока нет.</div>`;
      return;
    }

    el.innerHTML = recent.map(m => UIRenderer._clubMatchCard(m, { showActions: false })).join('');
  }

  /** Состав клуба с личной статистикой. */
  static renderMyClubSquad(players, meta = {}, isLoading = false) {
    const el = document.getElementById('my-club-squad-container');
    if (!el) return;

    if (isLoading && (!players || players.length === 0)) {
      el.innerHTML = `<div class="club-placeholder">Загрузка состава...</div>`;
      return;
    }

    if (!players || players.length === 0) {
      el.innerHTML = `
        <div class="club-placeholder">
          Состав клуба ещё не заполнен. Он появится после первой заявки состава в боте.
        </div>
      `;
      return;
    }

    const topScorer = meta.top_scorer ? meta.top_scorer.player_name : null;
    const topAssistant = meta.top_assistant ? meta.top_assistant.player_name : null;
    const topMvp = meta.top_mvp ? meta.top_mvp.player_name : null;

    const leadersHtml = (topScorer || topAssistant || topMvp) ? `
      <div class="club-leaders">
        ${topScorer ? `
          <div class="club-leader-card">
            <span class="club-leader-label">⚽ Лучший бомбардир</span>
            <span class="club-leader-name">${escapeHtml(topScorer)}</span>
            <span class="club-leader-value">${meta.top_scorer.goals}</span>
          </div>
        ` : ''}
        ${topAssistant ? `
          <div class="club-leader-card">
            <span class="club-leader-label">👟 Лучший ассистент</span>
            <span class="club-leader-name">${escapeHtml(topAssistant)}</span>
            <span class="club-leader-value">${meta.top_assistant.assists}</span>
          </div>
        ` : ''}
        ${topMvp ? `
          <div class="club-leader-card">
            <span class="club-leader-label">👑 MVP Клуба</span>
            <span class="club-leader-name">${escapeHtml(topMvp)}</span>
            <span class="club-leader-value">${meta.top_mvp.mvp_count}</span>
          </div>
        ` : ''}
      </div>
    ` : '';

    const rowsHtml = players.map((p, idx) => {
      const isLeader = (topScorer && p.player_name === topScorer) ||
                       (topAssistant && p.player_name === topAssistant) ||
                       (topMvp && p.player_name === topMvp);
      return `
        <div class="club-player-row ${isLeader ? 'leader' : ''}">
          <span class="club-player-num">${idx + 1}</span>
          <div class="club-player-id">
            <span class="club-player-name">${escapeHtml(p.player_name)}</span>
            ${p.position ? `<span class="club-player-pos">${escapeHtml(p.position)}</span>` : ''}
          </div>
          <span class="club-stat-badge goals" title="Голы">⚽ ${p.goals || 0}</span>
          <span class="club-stat-badge assists" title="Ассисты">👟 ${p.assists || 0}</span>
          ${p.mvp_count ? `<span class="club-stat-badge mvp" title="Награды «Игрок матча»">👑 ${p.mvp_count}</span>` : ''}
        </div>
      `;
    }).join('');

    el.innerHTML = `
      ${leadersHtml}
      <div class="club-squad-list">${rowsHtml}</div>
      <div class="club-squad-note">
        Жёлтые и красные карточки в лиге пока не фиксируются.
      </div>
    `;
  }

  /** Переключение внутренних под-вкладок «Мой Клуб». */
  static renderMyClubSubTab(tab) {
    document.querySelectorAll('#my-club-subtabs .mc-subtab-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.clubTab === tab);
    });
    const panels = {
      matches: 'my-club-matches-container',
      squad: 'my-club-squad-container',
      history: 'my-club-history-container'
    };
    Object.entries(panels).forEach(([name, id]) => {
      const el = document.getElementById(id);
      if (el) el.style.display = (name === tab) ? '' : 'none';
    });
  }

  /** Модальное окно протокола матча с авторами голов и скриншотом. */
  static renderMatchProtocolModal(detail) {
    const container = document.getElementById('match-protocol-content');
    if (!container) return;

    if (!detail) {
      container.innerHTML = '<div class="modal-loading">Загрузка протокола...</div>';
      return;
    }

    const m = detail.match || detail;
    const events = m.events || [];
    const t1 = m.team1_name || m.player1_team || 'Хозяева';
    const t2 = m.team2_name || m.player2_team || 'Гости';
    const u1 = m.player1_username ? `@${m.player1_username}` : '';
    const u2 = m.player2_username ? `@${m.player2_username}` : '';
    const scoreStr = (m.player1_score !== null && m.player1_score !== undefined)
      ? `${m.player1_score} : ${m.player2_score}`
      : '— : —';
    const isFinished = ['confirmed', 'completed', 'finished'].includes(m.status);
    const photoUrl = m.photo_url || (m.has_photo ? `/api/matches/${m.id}/photo` : null);

    const goals = events.filter(e => e.event_type === 'goal');
    const assists = events.filter(e => e.event_type === 'assist');
    // Корона приходит из matches.mvp_player голым именем: клуб по ней не
    // определить, поэтому показываем только имя, без принадлежности.
    const mvp = (m.mvp_player || '').trim();

    container.innerHTML = `
      <div class="proto-card">
        <div class="proto-head">
          <span class="proto-round">
            ${escapeHtml(matchRoundLabel(m))}${m.tournament_type === 'cup' ? '' : ` · Дивизион ${m.division_id || 1}`}
          </span>
          <span class="proto-status" style="color: ${isFinished ? 'var(--color-success)' : 'var(--text-muted)'};">
            ${isFinished ? '✅ Завершён' : (m.status || 'Ожидает')}
          </span>
        </div>

        <div class="proto-teams">
          <div class="proto-team">
            ${renderTeamLogoHtml(t1, 38)}
            <div class="proto-team-name">${escapeHtml(t1)}</div>
            ${u1 ? `<div class="proto-team-user">${escapeHtml(u1)}</div>` : ''}
          </div>

          <div class="proto-score-box">
            <div class="proto-score">
              ${scoreStr}
            </div>
          </div>

          <div class="proto-team">
            ${renderTeamLogoHtml(t2, 38)}
            <div class="proto-team-name">${escapeHtml(t2)}</div>
            ${u2 ? `<div class="proto-team-user">${escapeHtml(u2)}</div>` : ''}
          </div>
        </div>
      </div>

      ${mvp ? `
        <div class="proto-mvp">
          <span class="proto-mvp-icon">👑</span>
          <div>
            <div class="proto-mvp-label">Игрок матча</div>
            <div class="proto-mvp-name">${escapeHtml(mvp)}</div>
          </div>
        </div>
      ` : ''}

      <!-- Events List (Goals & Assists) -->
      <div class="proto-events">
        <div class="proto-section-title">
          ⚽ События матча
        </div>
        ${events.length === 0 ? `
          <div class="proto-events-empty">
            События (голы и ассисты) не зафиксированы
          </div>
        ` : `
          <div class="proto-goals">
            ${goals.map(g => `
              <div class="proto-goal">
                <span class="proto-goal-player">⚽ ${escapeHtml(g.player_name)} ${g.count > 1 ? `(x${g.count})` : ''}</span>
                <span class="proto-goal-team">${escapeHtml(g.team_name)}</span>
              </div>
            `).join('')}
            ${assists.map(a => `
              <div class="proto-assist">
                <span class="proto-assist-player">👟 ${escapeHtml(a.player_name)} ${a.count > 1 ? `(x${a.count})` : ''}</span>
                <span class="proto-goal-team">${escapeHtml(a.team_name)}</span>
              </div>
            `).join('')}
          </div>
        `}
      </div>

      <!-- Screenshot Section -->
      <div class="proto-shot">
        <div class="proto-shot-title">
          <span>📸 Скриншот протокола</span>
          ${photoUrl ? `<span class="proto-shot-hint">Нажмите для увеличения</span>` : ''}
        </div>
        ${photoUrl ? `
          <div class="proto-shot-frame">
            <a href="${photoUrl}" target="_blank" rel="noopener noreferrer" class="proto-shot-link">
              <img src="${photoUrl}" alt="Скриншот матча" class="proto-shot-img">
            </a>
          </div>
        ` : `
          <div class="proto-shot-empty">
            Скриншот для этого матча не был прикреплен
          </div>
        `}
      </div>
    `;
  }
}
