from raybot import config
from raybot.model import db, POI, Location
from raybot.bot import bot, dp
from raybot.util import h, HTML, split_tokens, get_buttons, get_map, get_user, tr, DOW
from raybot.actions.poi import POI_EDIT_CB, POI_LIST_CB
from raybot.actions.messages import broadcast_str, broadcast
import re
import os
import logging
import random
import humanized_opening_hours as hoh
from aiosqlite import DatabaseError
from string import ascii_lowercase
from datetime import datetime
from typing import Dict, Union
from collections import Counter
from aiogram import types
from aiogram.utils.callback_data import CallbackData
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.utils.exceptions import TelegramAPIError, MessageToDeleteNotFound


HOUSE_CB = CallbackData('ehouse', 'hid')
FLOOR_CB = CallbackData('efloor', 'floor')
BOOL_CB = CallbackData('boolattr', 'attr', 'value')
PHOTO_CB = CallbackData('ephoto', 'name', 'which')
TAG_CB = CallbackData('etag', 'tag')
TAG_PAGE_CB = CallbackData('tagpg', 'page')


class EditState(StatesGroup):
    name = State()
    location = State()
    keywords = State()
    confirm = State()
    attr = State()
    comment = State()
    message = State()


def cancel_keyboard():
    return types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton('❌ ' + tr('cancel'), callback_data='cancel')
    )


def location_keyboard():
    return types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton(
            tr(('editor', 'latlon')),
            url='https://zverik.github.io/latlon/#16/53.9312/27.6525'),
        types.InlineKeyboardButton('❌ ' + tr('cancel'), callback_data='cancel'),
    )


def valid_location(loc):
    bbox = config.BBOX
    if not bbox or len(bbox) != 4:
        return True
    return bbox[0] <= loc.lon <= bbox[2] and bbox[1] <= loc.lat <= bbox[3]


@dp.callback_query_handler(state=EditState.all_states, text='cancel')
async def new_cancel(query: types.CallbackQuery, state: FSMContext):
    await delete_msg(query, state)
    await state.finish()
    user = await get_user(query.from_user)
    if user.review:
        kbd = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton(
            '🗒️ ' + tr(('review', 'continue')), callback_data='continue_review'))
    else:
        kbd = get_buttons()
    await bot.send_message(
        query.from_user.id,
        tr(('new_poi', 'cancel')),
        reply_markup=kbd
    )


@dp.callback_query_handler(state='*', text='new')
async def new_poi(query: types.CallbackQuery):
    if config.MAINTENANCE:
        await bot.send_message(query.from_user.id, tr('maintenance'))
        return
    await EditState.name.set()
    await bot.send_message(
        query.from_user.id,
        tr(('new_poi', 'name')),
        reply_markup=cancel_keyboard()
    )


@dp.callback_query_handler(POI_EDIT_CB.filter(), state='*')
async def edit_poi(query: types.CallbackQuery, callback_data: Dict[str, str],
                   state: FSMContext):
    if config.MAINTENANCE:
        await bot.send_message(query.from_user.id, tr('maintenance'))
        return
    if callback_data['d'] == '1':
        await delete_msg(query)
    poi = await db.get_poi_by_id(int(callback_data['id']))
    await state.set_data({'poi': poi})
    await EditState.confirm.set()
    await print_edit_options(query.from_user, state)


@dp.message_handler(state=EditState.name)
async def new_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 3:
        await message.answer(tr(('new_poi', 'name_too_short')))
        return
    await state.set_data({'name': name})
    await EditState.location.set()
    await message.answer(tr(('new_poi', 'location')), reply_markup=location_keyboard())


def parse_location(message: types.Message):
    ll = re.match(r'^\s*(-?\d+\.\d+),\s*(-?\d+\.\d+)\s*$', message.text or '')
    if message.location:
        return Location(lon=message.location.longitude, lat=message.location.latitude)
    elif ll:
        return Location(lat=float(ll.group(1)), lon=float(ll.group(2)))
    return None


@dp.message_handler(state=EditState.location,
                    content_types=[types.ContentType.TEXT, types.ContentType.LOCATION])
async def new_location(message: types.Message, state: FSMContext):
    loc = parse_location(message)
    if not loc:
        await message.answer(tr(('new_poi', 'no_location')),
                             reply_markup=location_keyboard())
        return
    if not valid_location(loc):
        await message.answer(tr(('new_poi', 'location_out')),
                             reply_markup=location_keyboard())
        return
    await state.update_data(lon=loc.lon, lat=loc.lat)
    await EditState.keywords.set()
    await message.answer(tr(('new_poi', 'keywords')), reply_markup=cancel_keyboard())


@dp.message_handler(state=EditState.keywords)
async def new_keywords(message: types.Message, state: FSMContext):
    keywords = split_tokens(message.text)
    if not keywords:
        await message.answer(tr(('new_poi', 'no_keywords')))
        return
    # Create a POI
    data = await state.get_data()
    poi = POI(
        name=data['name'],
        location=Location(lat=data['lat'], lon=data['lon']),
        keywords=' '.join(keywords)
    )
    await state.set_data({'poi': poi})
    await EditState.confirm.set()
    await print_edit_options(message.from_user, state, tr(('new_poi', 'confirm')))


def format(v, yes=None, no=None, null=None):
    if v is None or v == '':
        return f'<i>{null or tr(("editor", "unknown"))}</i>'
    if isinstance(v, str):
        return h(v)
    if isinstance(v, bool):
        return (yes or tr(('editor', 'bool_yes'))) if v else (no or tr(('editor', 'bool_no')))
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, hoh.OHParser):
        return h(v.field)
    if isinstance(v, Location):
        return f'v.lat, v.lon'
    return str(v)


def new_keyboard():
    return types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton('💾 ' + tr('save'), callback_data='save'),
        types.InlineKeyboardButton('❌ ' + tr('cancel'), callback_data='cancel')
    )


async def print_edit_options(user: types.User, state: FSMContext, comment=None):
    poi = (await state.get_data())['poi']
    lines = []
    m = tr(('editor', 'panel'))
    lines.append(f'<b>{format(poi.name)}</b>')
    lines.append('')
    lines.append(f'/edesc <b>{m["desc"]}:</b> {format(poi.description, null=m["none"])}')
    lines.append(f'/ekey <b>{m["keywords"]}:</b> {format(poi.keywords)}')
    lines.append(f'/etag <b>{m["tag"]}:</b> {format(poi.tag)}')
    lines.append(f'/ehouse <b>{m["house"]}:</b> {format(poi.house_name)}')
    lines.append(f'/efloor <b>{m["floor"]}:</b> {format(poi.floor)}')
    lines.append(f'/eaddr <b>{m["addr"]}:</b> {format(poi.address_part)}')
    lines.append(f'/ehour <b>{m["hours"]}:</b> {format(poi.hours_src)}')
    lines.append(f'/eloc <b>{m["loc"]}:</b> '
                 '<a href="https://zverik.github.io/latlon/#18/'
                 f'{poi.location.lat}/{poi.location.lon}">'
                 f'{m["loc_browse"]}</a>')
    lines.append(f'/ephone <b>{m["phone"]}:</b> {format("; ".join(poi.phones))}')
    lines.append(f'/ewifi <b>{m["wifi"]}:</b> {format(poi.has_wifi)}')
    lines.append(f'/ecard <b>{m["card"]}:</b> {format(poi.accepts_cards)}')
    if poi.links:
        links = ', '.join([f'<a href="{l[1]}">{h(l[0])}</a>' for l in poi.links])
    else:
        links = f'<i>{m["none"]}</i>'
    lines.append(f'/elink <b>{m["links"]}:</b> {links}')
    lines.append(f'/ecom <b>{m["comment"]}:</b> {format(poi.comment, null=m["none"])}')
    if poi.photo_out and poi.photo_in:
        photos = m['photo_both']
    elif poi.photo_out:
        photos = m['photo_out']
    elif poi.photo_in:
        photos = m['photo_in']
    else:
        photos = m['none']
    lines.append(f'<b>{m["photo"]}:</b> {photos} ({m["photo_comment"]})')
    if poi.id:
        if poi.delete_reason:
            lines.append(f'<b>{m["deleted"]}:</b> {format(poi.delete_reason)}. '
                         f'{m["restore"]}: /undelete')
        else:
            lines.append(f'🗑️ {m["delete"]}: /delete')
        lines.append(f'✉️ {m["msg"]}: /msg')

    content = '\n'.join(lines)
    if comment is None:
        comment = tr(('new_poi', 'confirm2'))
    if comment:
        content += '\n\n' + h(comment)
    reply = await bot.send_message(
        user.id, content, parse_mode=HTML, reply_markup=new_keyboard(),
        disable_web_page_preview=True)
    await state.update_data(reply=reply.message_id)


def cancel_attr_kbd():
    return types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton(tr(('editor', 'cancel')), callback_data='cancel_attr')
    )


def edit_loc_kbd(poi):
    return types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton(
            tr(('editor', 'latlon')),
            url='https://zverik.github.io/latlon/#18/'
                f'{poi.location.lat}/{poi.location.lon}"'),
        types.InlineKeyboardButton(tr(('editor', 'cancel')), callback_data='cancel_attr')
    )


def boolean_kbd(attr: str):
    return types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton(tr(('editor', 'bool_true')),
                                   callback_data=BOOL_CB.new(attr=attr, value='true')),
        types.InlineKeyboardButton(tr(('editor', 'bool_false')),
                                   callback_data=BOOL_CB.new(attr=attr, value='false')),
        types.InlineKeyboardButton(tr(('editor', 'bool_none')),
                                   callback_data=BOOL_CB.new(attr=attr, value='null')),
        types.InlineKeyboardButton(tr(('editor', 'cancel')), callback_data='cancel_attr')
    )


def tag_kbd(page: int = 1):
    ROWS = 5
    tags = config.TAGS['suggest_tags']
    kbd = types.InlineKeyboardMarkup(row_width=3)
    if (page - 1) * ROWS * 3 >= len(tags):
        page = 1
    for tag in tags[(page - 1) * ROWS * 3:page * ROWS * 3]:
        kbd.insert(types.InlineKeyboardButton(config.TAGS['tags'].get(tag, [tag])[0],
                                              callback_data=TAG_CB.new(tag=tag)))
    kbd.add(
        types.InlineKeyboardButton(tr(('editor', 'cancel')), callback_data='cancel_attr'),
        types.InlineKeyboardButton(tr(('editor', 'next_page')) + ' ⏭️',
                                   callback_data=TAG_PAGE_CB.new(page=str(page + 1)))
    )
    return kbd


@dp.callback_query_handler(TAG_PAGE_CB.filter(), state=EditState.attr)
async def next_page(query: types.CallbackQuery, callback_data: Dict[str, str]):
    kbd = tag_kbd(int(callback_data['page']))
    await bot.edit_message_reply_markup(
        query.from_user.id, query.message.message_id, reply_markup=kbd)


async def delete_msg(source: Union[types.Message, types.CallbackQuery],
                     message_id: Union[int, FSMContext] = None):
    user_id = source.from_user.id
    if isinstance(message_id, FSMContext):
        message_id = (await message_id.get_data()).get('reply')
    if isinstance(source, types.CallbackQuery):
        if isinstance(message_id, list):
            message_id.append(source.message.message_id)
        else:
            message_id = source.message.message_id

    if message_id:
        if not isinstance(message_id, list):
            message_id = [message_id]
        for msg_id in message_id:
            if msg_id:
                try:
                    await bot.delete_message(user_id, msg_id)
                except MessageToDeleteNotFound:
                    pass
                except TelegramAPIError:
                    logging.exception('Failed to delete bot message')


@dp.message_handler(commands='ephoto', state=EditState.confirm)
async def show_photos(message: types.Message, state: FSMContext):
    poi = (await state.get_data())['poi']
    for photo, where in [(poi.photo_out, 'photo_out'), (poi.photo_in, 'photo_in')]:
        if photo:
            path = os.path.join(config.PHOTOS, photo + '.jpg')
            if os.path.exists(path):
                kbd = types.InlineKeyboardMarkup().add(
                    types.InlineKeyboardButton(
                        '🗑️ ' + tr(('editor', 'photo_del')),
                        callback_data=PHOTO_CB.new(name=photo, which='unlink')),
                    types.InlineKeyboardButton(
                        tr(('editor', 'cancel')), callback_data='cancel_attr')
                )
                await message.answer_photo(
                    types.InputFile(path), caption=tr(('editor', where)), reply_markup=kbd)


@dp.message_handler(commands='eout', state=EditState.confirm)
async def suggest_photo_out(message: types.Message, state: FSMContext):
    poi = (await state.get_data())['poi']
    pois = await db.get_poi_around(poi.location, 10, floor=poi.floor)
    photos = [p.photo_out for p in pois if p.photo_out]
    if not photos:
        await message.answer(tr(('editor', 'no_photos_around')))
        return

    photo_dist = {
        name: min(poi.location.distance(p.location) for p in pois if p.photo_out == name)
        for name in photos
    }
    photo_cnt = Counter(photos)
    photos = sorted(photo_cnt, key=lambda p: (int(photo_dist.get(p, 1000) / 10),
                                              100 - photo_cnt[p]))
    photos = photos[:3]

    kbd = types.InlineKeyboardMarkup(row_width=5)
    media = types.MediaGroup()
    for i, photo in enumerate(photos, 1):
        path = os.path.join(config.PHOTOS, photo + '.jpg')
        if os.path.exists(path):
            file_ids = await db.find_file_ids({photo: os.path.getsize(path)})
            if photo in file_ids:
                media.attach_photo(file_ids[photo])
            else:
                media.attach_photo(types.InputFile(path))
            kbd.insert(types.InlineKeyboardButton(
                str(i), callback_data=PHOTO_CB.new(name=photo, which='out')))
    kbd.insert(types.InlineKeyboardButton(
        tr(('editor', 'cancel')), callback_data='cancel_attr'))
    await delete_msg(message, state)
    msg = await bot.send_media_group(message.from_user.id, media=media)
    msg2 = await message.answer(tr(('editor', 'choose_photo')), reply_markup=kbd)
    replies = msg.message_id if not isinstance(msg, list) else [m.message_id for m in msg]
    replies.append(msg2.message_id)
    await state.update_data(reply=replies)


@dp.message_handler(state=EditState.confirm, content_types=types.ContentType.PHOTO)
async def upload_photo(message: types.Message, state: FSMContext):
    file_id = message.photo[-1].file_id
    name = await db.find_path_for_file_id(file_id)
    downloaded = False
    if not name:
        try:
            f = await bot.get_file(file_id)
            name = (''.join(random.sample(ascii_lowercase, 4)) +
                    datetime.now().strftime('%y%m%d%H%M%S'))
            path = os.path.join(config.PHOTOS, name + '.jpg')
            await f.download(path)
        except TelegramAPIError:
            logging.exception('Image upload fail')
            await message.answer(tr(('editor', 'upload_fail')))
            return
        if not os.path.exists(path):
            await message.answer(tr(('editor', 'upload_fail')))
            return
        await db.store_file_id(name, os.path.getsize(path), file_id)
        downloaded = True

    kbd = types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton(tr(('editor', 'photo_out')),
                                   callback_data=PHOTO_CB.new(name=name, which='out')),
        types.InlineKeyboardButton(tr(('editor', 'photo_in')),
                                   callback_data=PHOTO_CB.new(name=name, which='in')),
        types.InlineKeyboardButton(
            '🗑️ ' + tr(('editor', 'photo_del')),
            callback_data=PHOTO_CB.new(name=name, which='del' if downloaded else 'skip'))
    )
    await delete_msg(message, state)
    await message.answer(tr(('editor', 'photo')), reply_markup=kbd)


@dp.callback_query_handler(PHOTO_CB.filter(), state=EditState.confirm)
async def store_photo(query: types.CallbackQuery, callback_data: Dict[str, str],
                      state: FSMContext):
    poi = (await state.get_data())['poi']
    name = callback_data['name']
    path = os.path.join(config.PHOTOS, name + '.jpg')
    if not os.path.exists(path):
        await query.answer(tr(('editor', 'photo_lost')))
        return
    which = callback_data['which']
    if which == 'out':
        poi.photo_out = name
    elif which == 'in':
        poi.photo_in = name
    elif which == 'unlink':
        if poi.photo_out == name:
            poi.photo_out = None
        elif poi.photo_in == name:
            poi.photo_in = None
    elif which == 'del':
        os.remove(path)
        await query.answer(tr(('editor', 'photo_deleted')))
    else:
        await query.answer(tr(('editor', 'photo_forgot')))
    await delete_msg(query)
    await state.set_data({'poi': poi})
    await print_edit_options(query.from_user, state)


@dp.message_handler(commands='msg', state=EditState.confirm)
async def message_intro(message: types.Message, state: FSMContext):
    user = await get_user(message.from_user)
    if user.is_moderator():
        await message.answer(tr(('editor', 'cant_message')))
        return
    await delete_msg(message, state)
    reply = await message.answer(
        tr(('editor', 'message')), reply_markup=cancel_attr_kbd())
    await EditState.message.set()
    await state.update_data(reply=reply.message_id)


async def print_edit_message(message: types.Message, state: FSMContext,
                             attr: str, dash: bool = False, poi_attr: str = None,
                             msg_attr: str = None, kbd=None, value='-',
                             content: str = None):
    reply0 = None
    if value is not None:
        poi = (await state.get_data())['poi']
        if value == '-':
            pvalue = getattr(poi, poi_attr or attr)
        else:
            pvalue = value(poi)
        if pvalue:
            reply0 = (await message.answer(str(pvalue))).message_id

    if not content:
        content = tr(('editor', msg_attr or attr))
        if dash:
            content += ' ' + tr(('editor', 'dash'))
    if not kbd:
        kbd = cancel_attr_kbd()
    await delete_msg(message, state)
    reply = await message.answer(content, reply_markup=kbd, disable_web_page_preview=True)
    await EditState.attr.set()
    await state.update_data(attr=attr, reply=[reply.message_id, reply0])


@dp.message_handler(commands='ename', state=EditState.confirm)
async def edit_name(message: types.Message, state: FSMContext):
    await print_edit_message(message, state, 'name')


@dp.message_handler(commands='edesc', state=EditState.confirm)
async def edit_desc(message: types.Message, state: FSMContext):
    await print_edit_message(message, state, 'desc', dash=True, poi_attr='description')


@dp.message_handler(commands='etag', state=EditState.confirm)
async def edit_tag(message: types.Message, state: FSMContext):
    await print_edit_message(message, state, 'tag', dash=True, kbd=tag_kbd())


@dp.message_handler(commands='ecom', state=EditState.confirm)
async def edit_comment(message: types.Message, state: FSMContext):
    await print_edit_message(message, state, 'comment', dash=True)


@dp.message_handler(commands='ekey', state=EditState.confirm)
async def edit_keywords(message: types.Message, state: FSMContext):
    await print_edit_message(message, state, 'keywords')


@dp.message_handler(commands='eaddr', state=EditState.confirm)
async def edit_address(message: types.Message, state: FSMContext):
    await print_edit_message(message, state, 'address', dash=True, poi_attr='address_part')


@dp.message_handler(commands='eloc', state=EditState.confirm)
async def edit_location(message: types.Message, state: FSMContext):
    poi = (await state.get_data())['poi']
    await print_edit_message(message, state, 'location', value=None,
                             kbd=edit_loc_kbd(poi))


@dp.message_handler(commands='ephone', state=EditState.confirm)
async def edit_phones(message: types.Message, state: FSMContext):
    await print_edit_message(message, state, 'phones', dash=True,
                             value=lambda p: '; '.join(p.phones))


@dp.message_handler(commands='ehour', state=EditState.confirm)
async def edit_hours(message: types.Message, state: FSMContext):
    await print_edit_message(message, state, 'hours', dash=True,
                             poi_attr='hours_src')


@dp.message_handler(commands='ehouse', state=EditState.confirm)
async def edit_house(message: types.Message, state: FSMContext):
    poi = (await state.get_data())['poi']
    houses = await db.get_houses()
    houses.sort(key=lambda h: poi.location.distance(h.location))
    houses = houses[:3]

    # Prepare the map
    map_file = get_map([h.location for h in houses], ref=poi.location)
    # Prepare the keyboard
    kbd = types.InlineKeyboardMarkup(row_width=1)
    for i, house in enumerate(houses, 1):
        prefix = '✅ ' if house == poi.house else ''
        kbd.add(types.InlineKeyboardButton(
            f'{prefix} {i} {house.name}', callback_data=HOUSE_CB.new(hid=house.key)))
    kbd.add(types.InlineKeyboardButton(
        tr(('editor', 'cancel')), callback_data='cancel_attr'))

    # Finally send the reply
    await delete_msg(message, state)
    if map_file:
        await message.answer_photo(types.InputFile(map_file.name),
                                   caption=tr(('editor', 'house')), reply_markup=kbd)
        map_file.close()
    else:
        await message.answer(tr(('editor', 'house')), reply_markup=kbd)


@dp.callback_query_handler(HOUSE_CB.filter(), state=EditState.confirm)
async def update_house(query: types.CallbackQuery, callback_data: Dict[str, str],
                       state: FSMContext):
    poi = (await state.get_data())['poi']
    hid = callback_data['hid']
    poi.house = hid
    h_data = await db.get_poi_by_key(hid)
    if h_data:
        poi.house_name = h_data.name
    await delete_msg(query)
    await state.set_data({'poi': poi})
    await print_edit_options(query.from_user, state)


@dp.message_handler(commands='efloor', state=EditState.confirm)
async def edit_floor(message: types.Message, state: FSMContext):
    poi = (await state.get_data())['poi']
    floors = await db.get_floors_by_house(poi.house)
    if floors and floors != [None]:
        kbd = types.InlineKeyboardMarkup(row_width=3)
        for floor in floors:
            if floor is not None:
                kbd.insert(types.InlineKeyboardButton(
                    floor, callback_data=FLOOR_CB.new(floor=floor)))
        kbd.insert(types.InlineKeyboardButton(
            tr(('editor', 'cancel')), callback_data='cancel_attr'))
    else:
        kbd = cancel_attr_kbd()
    await print_edit_message(message, state, 'floor', dash=True,
                             kbd=kbd, value=None)


@dp.callback_query_handler(FLOOR_CB.filter(), state=EditState.attr)
async def update_floor(query: types.CallbackQuery, callback_data: Dict[str, str],
                       state: FSMContext):
    poi = (await state.get_data())['poi']
    floor = callback_data['floor']
    poi.floor = floor if floor != '-' else None
    await delete_msg(query)
    await EditState.confirm.set()
    await state.set_data({'poi': poi})
    await print_edit_options(query.from_user, state)


@dp.message_handler(commands='ewifi', state=EditState.confirm)
async def edit_wifi(message: types.Message, state: FSMContext):
    await delete_msg(message, state)
    reply = await message.answer(
        tr(('editor', 'wifi')), reply_markup=boolean_kbd('wifi'))
    await state.update_data(reply=reply.message_id)


@dp.message_handler(commands='ecard', state=EditState.confirm)
async def edit_cards(message: types.Message, state: FSMContext):
    await delete_msg(message, state)
    reply = await message.answer(
        tr(('editor', 'cards')), reply_markup=boolean_kbd('cards'))
    await state.update_data(reply=reply.message_id)


@dp.callback_query_handler(BOOL_CB.filter(), state=EditState.confirm)
async def update_boolean(query: types.CallbackQuery, callback_data: Dict[str, str],
                         state: FSMContext):
    poi = (await state.get_data())['poi']
    attr = callback_data['attr']
    svalue = callback_data['value']
    if svalue == 'null':
        value = None
    else:
        value = svalue == 'true'
    if attr == 'wifi':
        poi.has_wifi = value
    elif attr == 'cards':
        poi.accepts_cards = value
    else:
        query.answer(f'Unknown field {attr}')
    await delete_msg(query)
    await state.set_data({'poi': poi})
    await EditState.confirm.set()
    await print_edit_options(query.from_user, state)


@dp.callback_query_handler(TAG_CB.filter(), state=EditState.attr)
async def update_tag(query: types.CallbackQuery, callback_data: Dict[str, str],
                     state: FSMContext):
    poi = (await state.get_data())['poi']
    poi.tag = callback_data['tag']
    await delete_msg(query)
    await state.set_data({'poi': poi})
    await EditState.confirm.set()
    await print_edit_options(query.from_user, state)


@dp.message_handler(commands='elink', state=EditState.confirm)
async def edit_links(message: types.Message, state: FSMContext):
    poi = (await state.get_data())['poi']
    if poi.links:
        content = tr(('editor', 'links_have')) + '\n\n'
        content += '\n'.join([f'🔗 {h(l[0])}: {h(l[1])}' for l in poi.links])
    else:
        content = tr(('editor', 'no_links'))
    content += '\n\n' + tr(('editor', 'links'))
    await print_edit_message(message, state, 'links', value=None, content=content)


@dp.message_handler(commands='delete', state=EditState.confirm)
async def delete_poi_prompt(message: types.Message, state: FSMContext):
    poi = (await state.get_data())['poi']
    if poi.delete_reason:
        user = await get_user(message.from_user)
        if not user.is_moderator():
            await message.answer(tr(('editor', 'delete_twice')))
        else:
            await db.delete_poi_forever(user.id, poi)
            await state.finish()
            await message.answer(tr(('editor', 'deleted2')), reply_markup=get_buttons())
    else:
        await message.answer(tr(('editor', 'delete')), reply_markup=cancel_attr_kbd())
        await EditState.attr.set()
        await state.update_data(attr='delete')


@dp.message_handler(commands='undelete', state=EditState.confirm)
async def undelete_poi(message: types.Message, state: FSMContext):
    user = await get_user(message.from_user)
    if not user.is_moderator():
        await message.answer(tr(('editor', 'cant_restore')))
        return
    poi = (await state.get_data())['poi']
    await db.restore_poi(message.from_user.id, poi)
    await state.finish()
    if user.review:
        kbd = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton(
            '🗒️ ' + tr(('review', 'continue')), callback_data='continue_review'))
    else:
        kbd = get_buttons()
    await message.answer(tr(('editor', 'restored')), reply_markup=kbd)


@dp.callback_query_handler(text='cancel_attr', state=EditState.all_states)
async def cancel_attr(query: types.CallbackQuery, state: FSMContext):
    await delete_msg(query, state)
    await EditState.confirm.set()
    await print_edit_options(query.from_user, state)


@dp.message_handler(content_types=types.ContentType.LOCATION, state=EditState.attr)
async def store_location(message: types.Message, state: FSMContext):
    data = await state.get_data()
    poi = data['poi']
    attr = data['attr']
    if attr != 'location':
        return
    loc = parse_location(message)
    if not loc:
        await message.answer(tr(('new_poi', 'no_location')), reply_markup=edit_loc_kbd(poi))
        return
    poi.location = loc
    await delete_msg(message, state)
    await state.set_data({'poi': poi})
    await EditState.confirm.set()
    await print_edit_options(message.from_user, state)


RE_URL = re.compile(r'^https?://')
# TODO: use hours_abbr and DOW
RE_HOURS = re.compile(r'^(?:(пн|вт|ср|чт|пт|сб|вс|mo|tu|we|th|fr|sa|su)(?:\s*-?'
                      r'\s*(пн|вт|ср|чт|пт|сб|вс|mo|tu|we|th|fr|sa|su))?\s+)?'
                      r'(\d\d?(?:[:.]\d\d)?)\s*-\s*(\d\d(?:[:.]\d\d)?)'
                      r'(?:\s+об?е?д?\s+(\d\d?(?:[:.]\d\d)?)\s*-\s*(\d\d(?:[:.]\d\d)?))?$')


def parse_hours(s):
    def norm_hour(h):
        if not h:
            return None
        if len(h) < 4:
            h += ':00'
        return h.replace('.', ':').rjust(5, '0')

    if s in ('24', '24/7'):
        return '24/7'

    HOURS_WEEK = {tr(('editor', 'hours_abbr'))[i]: DOW[i] for i in range(7)}
    HOURS_WEEK.update({DOW[i].lower(): DOW[i] for i in range(7)})

    parts = []
    for part in s.split(','):
        m = RE_HOURS.match(part.strip().lower())
        if not m:
            raise ValueError(part)
        wd = 'Mo-Su' if not m.group(1) else HOURS_WEEK[m.group(1)]
        if m.group(2):
            wd += '-' + HOURS_WEEK[m.group(2)]
        h1 = norm_hour(m.group(3))
        h2 = norm_hour(m.group(4))
        l1 = norm_hour(m.group(5))
        l2 = norm_hour(m.group(6))
        if l1:
            wd += f' {h1}-{l1},{l2}-{h2}'
        else:
            wd += f' {h1}-{h2}'
        parts.append(wd)
    return '; '.join(parts)


def parse_link(value):
    parts = value.lower().replace('. ', '.').split(None, 1)
    if len(parts) == 1 and '.' not in parts[0]:
        return parts
    if len(parts) == 1:
        parts = [tr('default_link'), parts[0]]
    REPLACE_TITLE = tr(('editor', 'link_replace'))
    REPLACE_TITLE['instagram'] = tr(('editor', 'instagram'))
    if parts[0] in REPLACE_TITLE:
        parts[0] = REPLACE_TITLE[parts[0]]
    if parts[0] == tr(('editor', 'instagram')) and 'instagram.' not in parts[1]:
        parts[1] = 'https://instagram.com/' + parts[1]
    elif parts[0] == 'vk' and 'vk.' not in parts[1]:
        parts[1] = 'https://vk.com/' + parts[1]
    if '://' not in parts[1]:
        parts[1] = 'https://' + parts[1]
    return parts


@dp.message_handler(state=EditState.attr)
async def store_attr(message: types.Message, state: FSMContext):
    data = await state.get_data()
    poi = data['poi']
    attr = data['attr']
    value = message.text.strip()

    if attr == 'name':
        if value == '-':
            await message.answer(tr(('editor', 'empty_name')), reply_markup=cancel_attr_kbd())
            return
        poi.name = value
    elif attr == 'desc':
        poi.description = None if value == '-' else value
    elif attr == 'comment':
        poi.comment = None if value == '-' else value
    elif attr == 'floor':
        poi.floor = None if value == '-' else value
    elif attr == 'tag':
        if value == '-':
            poi.tag = None
        else:
            parts = [p.strip() for p in re.split(r'[ =]+', value.lower().replace('-', '_'))]
            if len(parts) != 2 or not re.match(r'^[a-z]+$', parts[0]):
                await message.answer(tr(('editor', 'tag_format'), value),
                                     reply_markup=cancel_attr_kbd())
                return
            poi.tag = '='.join(parts)
    elif attr == 'keywords':
        new_kw = split_tokens(value)
        if new_kw:
            old_kw = [] if not poi.keywords else poi.keywords.split()
            poi.keywords = ' '.join(old_kw + new_kw)
    elif attr == 'address':
        poi.address_part = None if value == '-' else value
    elif attr == 'location':
        loc = parse_location(message)
        if not loc:
            await message.answer(tr(('new_poi', 'no_location')),
                                 reply_markup=edit_loc_kbd(poi))
            return
        if not valid_location(loc):
            await message.answer(tr(('new_poi', 'location_out')),
                                 reply_markup=edit_loc_kbd(poi))
            return
        poi.location = loc
    elif attr == 'hours':
        if value == '-':
            poi.hours = None
            poi.hours_src = None
        else:
            try:
                hours = parse_hours(value)
            except ValueError as e:
                await message.answer(tr(('editor', 'hours_format'), e),
                                     reply_markup=cancel_attr_kbd())
                return
            poi.hours_src = hours
            poi.hours = hoh.OHParser(hours)
    elif attr == 'phones':
        if not value or value == '-':
            poi.phones = []
        else:
            poi.phones = [p.strip() for p in re.split(r'[;,]', value)]
    elif attr == 'links':
        if value:
            parts = parse_link(value)
            if parts:
                if len(parts) == 1:
                    poi.links = [l for l in poi.links if l[0] != parts[0]]
                else:
                    found = False
                    for i, l in enumerate(poi.links):
                        if l[0] == parts[0]:
                            found = True
                            l[1] = parts[1]
                    if not found:
                        poi.links.append(parts)
    elif attr == 'delete':
        await db.delete_poi(message.from_user.id, poi, value)
        await state.finish()
        user = await get_user(message.from_user)
        if user.review:
            kbd = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton(
                '🗒️ ' + tr(('review', 'continue')), callback_data='continue_review'))
        else:
            kbd = get_buttons()
        await message.answer(tr(('editor', 'deleted')), reply_markup=kbd)
        await broadcast_str(tr(('editor', 'just_deleted'), id=poi.id, reason=value),
                            message.from_user.id)
        return
    else:
        await message.answer(tr(('editor', 'wrong_attr'), attr))

    await delete_msg(message, state)
    await state.set_data({'poi': poi})
    await EditState.confirm.set()
    await print_edit_options(message.from_user, state)


@dp.message_handler(state=EditState.confirm)
async def other_msg(message: types.Message, state: FSMContext):
    await message.answer(tr(('editor', 'other_msg')))


@dp.message_handler(state=EditState.message)
async def send_message(message: types.Message, state: FSMContext):
    user = await get_user(message.from_user)
    poi = (await state.get_data())['poi']
    if poi.id is None:
        # Won't add an abstract message to the queue
        await broadcast(message)
    else:
        await db.add_to_queue(user, poi, message.text)
    await state.finish()
    await message.answer(tr(('editor', 'msg_sent')))


@dp.callback_query_handler(state=EditState.confirm, text='save')
async def new_save(query: types.CallbackQuery, state: FSMContext):
    poi = (await state.get_data())['poi']

    # If not a moderator, mark this as needs check
    user = await get_user(query.from_user)
    if not user.is_moderator() and poi.id is None:
        poi.needs_check = True

    # Send the POI to the database
    poi_id = poi.id
    try:
        if user.is_moderator() or poi.id is None:
            poi_id = await db.insert_poi(query.from_user.id, poi)
            saved = 'saved'
            if not user.is_moderator():
                await broadcast_str(tr(('editor', 'just_added'), id=poi.id, name=poi.name),
                                    query.from_user.id)
        else:
            await db.add_to_queue(user, poi)
            await broadcast_str(tr(('queue', 'added')))
            saved = 'sent'
    except DatabaseError as e:
        await bot.send_message(
            query.from_user.id, tr(('editor', 'error_save'), e), reply_markup=new_keyboard())
        return

    # Reset state and thank the user
    await delete_msg(query, state)
    await state.finish()
    kbd = types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton('👀 ' + tr(('editor', 'saved_look')),
                                   callback_data=POI_LIST_CB.new(id=poi_id)),
        types.InlineKeyboardButton('➕ ' + tr(('editor', 'saved_add')), callback_data='new')
    )
    if user.review:
        kbd.insert(types.InlineKeyboardButton(
            '🗒️ ' + tr(('review', 'continue')), callback_data='continue_review'))
    await bot.send_message(query.from_user.id, tr(('editor', saved)), reply_markup=kbd)
