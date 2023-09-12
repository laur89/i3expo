#!/usr/bin/python3
#
# dependencies:
#    pip3 install --user -r ./requirements.txt
#
# add i3 conf:
#   exec_always --no-startup-id i3expod.py
#   for_window [class="^i3expod\.py$"] fullscreen enable
#   bindsym $mod1+e exec --no-startup-id killall -s SIGUSR1 i3expod.py

import ctypes
import os
import configparser
import xdg
import pygame
import i3ipc
import copy
import signal
import traceback
import pprint
import time
import math
from debounce import Debounce
from functools import partial
from threading import Thread
from PIL import Image, ImageDraw
import pulp

from xdg.BaseDirectory import xdg_config_home

pp = pprint.PrettyPrinter(indent=4)

global_updates_running = True
global_knowledge = {'active': -1}  # 'active' = currently active ws num

qm_cache = {}  # screen_w x screen_h mapped against rendered question mark

i3 = i3ipc.Connection()

config_file = os.path.join(xdg_config_home, 'i3expo', 'config')
screenshot_lib = 'prtscn.so'
screenshot_lib_path = os.path.dirname(os.path.realpath(__file__)) + os.path.sep + screenshot_lib
grab = ctypes.CDLL(screenshot_lib_path)
grab.getScreen.argtypes = []
blacklist_classes = ['i3expod.py']  # if focused, don't grab screenshot
loop_interval = 100.0  # will be overwritten by config


def shutdown_common():
    print('Shutting down...')

    try:
        pygame.display.quit()
        pygame.quit()
    finally:
        os._exit(0)


def signal_quit(signal, stack_frame):
    i3.main_quit()
    shutdown_common()


def on_shutdown(i3conn, e):
    # if e.change == 'restart':
        # self.persist_state()
    shutdown_common()


def signal_reload(signal, stack_frame):
    global loop_interval

    read_config()

    # re-define global vars populated from config:
    loop_interval = config.getfloat('CONF', 'forced_update_interval_sec')


def should_show_ui():
    return len(global_knowledge) - 1 > 1


def signal_toggle_ui(signal, stack_frame):
    global global_updates_running

    if not global_updates_running:  # UI toggle
        global_updates_running = True
    elif should_show_ui():
        # i3.command('workspace i3expod-temporary-workspace')  # jump to temp ws; doesn't seem to work well in multimon setup; introduced by  https://gitlab.com/d.reis/i3expo/-/commit/d14685d16fd140b3a7374887ca086ea66e0388f5 - looks like it solves problem where fullscreen state is lost on expo toggle
        global_updates_running = False
        updater_debounced.reset()

        # ui_thread = Thread(target = show_ui)
        # ui_thread.daemon = True
        show_ui()


def get_color(raw):
    return pygame.Color(raw)


def read_config():
    config.read_dict({
        'CONF': {
            'bgcolor'                    : 'gray20',
            'frame_active_color'         : '#5a6da4',
            'frame_inactive_color'       : '#93afb3',
            'frame_missing_color'        : '#ffe6d0',
            'tile_missing_color'         : 'gray40',

            'padding_percent_x'          : 5,
            'padding_percent_y'          : 5,
            'spacing_percent_x'          : 4,
            'spacing_percent_y'          : 4,
            'frame_width_px'             : 3,

            'forced_update_interval_sec' : 10.0,
            'debounce_period_sec'        : 1.0,

            'names_show'                 : True,
            'names_font'                 : 'verdana',  # list with pygame.font.get_fonts()
            'names_fontsize'             : 25,
            'names_color'                : 'white',
            'highlight_percentage'       : 20
        }
    })

    # write config file down if not existing:
    root_dir = os.path.dirname(config_file)
    if not os.path.exists(root_dir):
        os.makedirs(root_dir)

    if os.path.exists(config_file):
        config.read(config_file)
    else:
        with open(config_file, 'w') as f:
            config.write(f)


def grab_screen(i):
    # print('GRABBING FOR: {}'.format(i['name']))
    w = i['w']
    h = i['h']

    size = w * h
    objlength = size * 3

    result = (ctypes.c_ubyte*objlength)()

    grab.getScreen(i['x'], i['y'], w, h, result)
    return result


def update_workspace(ws, focused_ws):
    i = global_knowledge.get(ws.num)
    if i is None:
        i = global_knowledge[ws.num] = {
            'name'        : ws.name,
            'screenshot'  : None,  # byte-array representation of this ws screenshot
            'last-update' : 0.0,   # unix epoch when ws was last grabbed
            'state'       : 0,     # numeric representation of current state of ws - windows and their sizes
            'x'           : 0,
            'y'           : 0,
            'w'           : 0,
            'h'           : 0,
            'ratio'       : 0,
            'windows'     : {}    # TODO unused atm
        }

    # always update dimensions; eg ws might've been moved onto a different output:
    i['x'] = ws.rect.x
    i['y'] = ws.rect.y
    i['w'] = ws.rect.width
    i['h'] = ws.rect.height
    i['ratio'] = ws.rect.width / ws.rect.height

    if ws.id == focused_ws.id:
        # print('active WS:: {}'.format(ws.name))
        global_knowledge['active'] = ws.num


def init_knowledge():
    tree = i3.get_tree()
    focused_ws = tree.find_focused().workspace()

    for ws in tree.workspaces():
        # print('workspaces() num {} name [{}], focused {}'.format(ws.num, ws.name, ws.focused))
        update_workspace(ws, focused_ws)


# ! Note calling this function will also store the current state in global_knowledge!
# TODO: this will likely be deprecaated when/if i3 implements 'resize' event. actually... we now also track window title changes.
def tree_has_changed(focused_ws):
    state = 0
    for con in focused_ws.leaves():
        f = 31 if con.focused else 0  # so focus change can be detected
        # add following if you want window title to be included in the state:
        # abs(hash(con.name)) % 10_000
        # or: hash(con.name) % 10_000  (if neg values are ok)
        state += con.id % (con.rect.x + con.rect.y + con.rect.width + con.rect.height + hash(con.name) % 10_000 + f)

    if global_knowledge[focused_ws.num]['state'] == state:
        return False
    global_knowledge[focused_ws.num]['state'] = state

    return True


def should_update(rate_limit_period, focused_con, focused_ws, con_tree, event, force, only_focused_win):
    if not global_updates_running: return False
    elif rate_limit_period is not None and time.time() - global_knowledge[focused_ws.num]['last-update'] <= rate_limit_period: return False
    elif focused_con.window_class in blacklist_classes: return False
    elif only_focused_win and not event.container.focused:  # note assumes WindowEvent
    # elif only_focused_win and focused_con.id != event.container.id:  # note assumes WindowEvent
        return False
    elif force:
        tree_has_changed(focused_ws)  # call it, as we still want to store 'state' value if changed
        updater_debounced.reset()
        return True
    elif not tree_has_changed(focused_ws):
        return False

    return True


def update_state(i3, e=None, rate_limit_period=None,
                 force=False, debounced=False,
                 all_active_ws=False, only_focused_win=False):
    # print('TOGGLING updat_state() by event [{}]; force: {}, debounced: {}'.format(e.change if e else 'None', force, debounced))

    time.sleep(0.2)  # TODO system-specific; configurize? also, maybe only sleep if it's _not_ debounced?

    # t0 = time.time()
    tree = i3.get_tree()
    focused_con = tree.find_focused()
    focused_ws = focused_con.workspace()
    workspaces = tree.workspaces()

    wss = []
    if all_active_ws:
        ws_list = [output.current_workspace for output in i3.get_outputs() if output.active]  # TODO: cache on OutputEvent? or maybe we could use some 'visible' attr of a ws instead of querying for outputs?
        for ws in workspaces:
            if ws.name in ws_list:
                wss.append(ws)
    else:  # update/process only the currently focused ws
        wss.append(focused_ws)

    for ws in wss:
        update_workspace(ws, focused_ws)
        if should_update(rate_limit_period, focused_con, ws, tree, e, force, only_focused_win):
            i = global_knowledge[ws.num]
            # t0 = time.time()
            i['screenshot'] = grab_screen(i)
            # print('grabbing image took {}'.format(time.time()-t0))
            i['last-update'] = time.time()

    wspace_nums = [w.num for w in workspaces]
    deleted = [n for n in global_knowledge if type(n) is int and n not in wspace_nums]  # TODO move n-keys to different map, so type(n)=int check wouldn't be necessary?
    for n in deleted:
        del global_knowledge[n]

    # print('whole update_state() took {}'.format(time.time()-t0))


def get_hovered_tile(mpos, tiles):
    for tile in tiles:
        t = tiles[tile]
        if (mpos[0] >= t['ul'][0]
                and mpos[0] <= t['br'][0]
                and mpos[1] >= t['ul'][1]
                and mpos[1] <= t['br'][1]):
            return tile
    return None


def show_ui():
    global global_updates_running

    frame_active_color = config.getcolor('CONF', 'frame_active_color')
    frame_inactive_color = config.getcolor('CONF', 'frame_inactive_color')
    frame_missing_color = config.getcolor('CONF', 'frame_missing_color')
    tile_missing_color = config.getcolor('CONF', 'tile_missing_color')

    pygame.display.init()
    pygame.font.init()

    ws = global_knowledge[global_knowledge['active']]

    screen = pygame.display.set_mode((ws['w'], ws['h']), pygame.RESIZABLE)
    pygame.display.set_caption('i3expo')

    grid_layout = resolve_grid_layout(ws['w'], ws['h'])

    tiles = {}  # contains grid tile index to thumbnail/ws_screenshot data mappings
    active_tile = None

    wss = [n for n in global_knowledge if type(n) is int]
    wss.sort()

    grid = []

    # compose the grid:
    for row_idx, elements_in_row in enumerate(grid_layout):
        no_of_previous_tiles = sum(grid_layout[:row_idx])
        row = []
        grid.append(row)

        for curr_tile_on_row_idx in range(elements_in_row):
            index = no_of_previous_tiles + curr_tile_on_row_idx
            ws_num = wss.pop(0)
            t = {
                'active'    : False,
                'mouseoff'  : None,
                'mouseon'   : None,
                'ul'        : (-1, -1),  # upper-left coords (including frame/border);
                'br'        : (-1, -1),  # bottom-right coords (including frame/border);
                'row_idx'   : row_idx,
                'ws'        : ws_num,        # workspace.num this tile represents;
                'frame_col' : None,
                'tile_col'  : None,
                'img'       : None  # processed, ie pygame-ready thumbnail;
            }
            tiles[index] = t
            row.append(t)

            ws_conf = global_knowledge[ws_num]

            if ws_conf['screenshot'] is not None:
                # t0 = time.time()
                t['img'] = process_img(ws_conf)
                # print('processing image took {}'.format(time.time()-t0))
                if global_knowledge['active'] == ws_num:
                    active_tile = index  # first highlight our current ws
                    t['frame_col'] = frame_active_color
                else:
                    t['frame_col'] = frame_inactive_color
            else:
                t['frame_col'] = frame_missing_color
                t['tile_col'] = tile_missing_color
                t['img'] = draw_missing_tile(ws_conf['w'], ws_conf['h'])


    draw_grid(screen, grid)
    pygame.display.flip()  # update full dispaly Surface on the screen
    input_event_loop(screen, tiles, active_tile, grid_layout)
    pygame.display.quit()
    pygame.quit()
    global_updates_running = True


def process_img(ws_conf):
    pil = Image.frombuffer('RGB', (ws_conf['w'], ws_conf['h']), ws_conf['screenshot'], 'raw', 'RGB', 0, 1)
    # return pygame.image.fromstring(pil.tobytes(), pil.size, pil.mode)
    return pygame.image.frombuffer(pil.tobytes(), pil.size, pil.mode)  # frombuffer() potentially faster than .fromstring()


def draw_missing_tile(screen_w, screen_h):
    key = '{}x{}'.format(screen_w, screen_h)

    if key in qm_cache:
        return qm_cache[key]

    missing_tile = pygame.Surface((screen_w, screen_h), pygame.SRCALPHA, 32)
    missing_tile = missing_tile.convert_alpha()
    qm = pygame.font.SysFont('sans-serif', screen_h).render('?', True, (150, 150, 150))
    qm_size = qm.get_rect().size
    origin_x = round((screen_w - qm_size[0])/2)
    origin_y = round((screen_h - qm_size[1])/2)
    missing_tile.blit(qm, (origin_x, origin_y))

    qm_cache[key] = missing_tile

    return missing_tile


def get_max_tile_dimensions(screen_w, screen_h, pad_w, pad_h, spacing_x, spacing_y, grid):
    # if (screen_w > screen_h):  # TODO
    max_row_len = max([len(row) for row in grid])
    r = screen_h / screen_w

    # find tile width:
    problem = pulp.LpProblem('optimalTileWidth', pulp.LpMaximize)
    max_tile_w = pulp.LpVariable('max_tile_w', lowBound = 0)
    problem += max_tile_w
    problem += r * len(grid) * max_tile_w + (len(grid)-1) * spacing_y + 2*pad_h <= screen_h
    problem += max_row_len*max_tile_w + (max_row_len-1)*spacing_x + 2*pad_w <= screen_w

    result = problem.solve()
    assert result == pulp.LpStatusOptimal
    max_tile_w = max_tile_w.value()
    max_tile_h = max_tile_w * r

    return max_tile_w, max_tile_h


def render_workspace_name(tile, screen, origin_x, origin_y, tile_w, tile_h):
    try:
        # check if name for given ws has been hardcoded in our config:
        name = config.get('CONF', 'workspace_' + str(tile['ws']))
    except:
        name = global_knowledge[tile['ws']]['name']

    highlight_percentage = config.getint('CONF', 'highlight_percentage')
    names_color = config.getcolor('CONF', 'names_color')
    names_font = config.get('CONF', 'names_font')
    names_fontsize = config.getint('CONF', 'names_fontsize')
    font = pygame.font.SysFont(names_font, names_fontsize)

    name = font.render(name, True, names_color)
    name_width = name.get_rect().size[0]
    name_x = origin_x + round((tile_w - name_width) / 2)
    name_y = origin_y + round(tile_h) + round(tile_h * 0.02)
    screen.blit(name, (name_x, name_y))


def draw_grid(screen, grid):
    padding_x = config.getint('CONF', 'padding_percent_x')
    padding_y = config.getint('CONF', 'padding_percent_y')
    spacing_x = config.getint('CONF', 'spacing_percent_x')
    spacing_y = config.getint('CONF', 'spacing_percent_y')
    frame_width = config.getint('CONF', 'frame_width_px')
    highlight_percentage = config.getint('CONF', 'highlight_percentage')

    screen_w = screen.get_width()
    screen_h = screen.get_height()

    pad_w = screen_w * padding_x / 100  # spacing between outermost tiles and screen
    pad_h = screen_h * padding_y / 100  # spacing between outermost tiles and screen
    spacing_x = screen_w * spacing_x / 100  # spacing between tiles
    spacing_y = screen_h * spacing_y / 100  # spacing between tiles


    screen.fill(config.getcolor('CONF', 'bgcolor'))

    max_tile_w, max_tile_h = get_max_tile_dimensions(screen_w, screen_h, pad_w, pad_h, spacing_x, spacing_y, grid)

    for i, row in enumerate(grid):
        # origin_y = round((screen_h - len(grid)*max_tile_h - (len(grid)-1)*spacing_y)/2) + round((max_tile_h + spacing_y) * i)
        center_y = ((screen_h - len(grid)*max_tile_h - (len(grid)-1)*spacing_y)/2) + ((max_tile_h + spacing_y) * i) + max_tile_h/2
        for j, t in enumerate(row):

            tile_h = max_tile_h  # reset
            tile_w = max_tile_w  # reset

            # origin_x = round((screen_w - len(row)*max_tile_w - (len(row)-1)*spacing_x)/2) + round((max_tile_w + spacing_x) * j)
            center_x = ((screen_w - len(row)*max_tile_w - (len(row)-1)*spacing_x)/2) + ((max_tile_w + spacing_x) * j) + max_tile_w/2

            ws_conf = global_knowledge[t['ws']]

            if (screen_w > screen_h):
                # height remains @ max
                tile_w = tile_h * ws_conf['ratio']
            else:
                # width remains @ max
                tile_h = tile_w / ws_conf['ratio']

            tile_w_rounded = round(tile_w)
            tile_h_rounded = round(tile_h)
            origin_y = center_y - tile_h/2
            origin_x = center_x - tile_w/2


            t['ul'] = (origin_x, origin_y)
            t['br'] = (origin_x + tile_w_rounded, origin_y + tile_h_rounded)

            screen.fill(t['frame_col'],
                    (
                        origin_x,
                        origin_y,
                        tile_w,
                        tile_h
                    ))
            if t['tile_col'] is not None:
                screen.fill(t['tile_col'],
                        (
                            origin_x + frame_width,
                            origin_y + frame_width,
                            tile_w - 2*frame_width,
                            tile_h - 2*frame_width
                        ))

            # draw ws thumbnail (note we need to adjust for frame/border width)
            screen.blit(
                    pygame.transform.smoothscale(t['img'], (tile_w_rounded - 2*frame_width, tile_h_rounded - 2*frame_width)),
                    (origin_x + frame_width, origin_y + frame_width)
            )

            if config.getboolean('CONF', 'names_show'):
                render_workspace_name(t, screen, origin_x, origin_y, tile_w, tile_h)

            mouseoff = screen.subsurface((origin_x, origin_y, tile_w_rounded, tile_h_rounded)).copy()  # used to replace mouseon highlight
            mouseon = mouseoff.copy()

            lightmask = pygame.Surface((tile_w_rounded, tile_h_rounded), pygame.SRCALPHA, 32)
            lightmask.convert_alpha()
            lightmask.fill((255,255,255,255 * highlight_percentage / 100))
            mouseon.blit(lightmask, (0, 0))
            t['mouseon'] = mouseon
            t['mouseoff'] = mouseoff


def resolve_grid_layout(screen_w, screen_h):
    l = len(global_knowledge) - 1
    grid = []
    max_tiles_per_row = 3 if screen_w >= screen_h else 2  # TODO: resolve from ratio?

    # TODO: need to start increasing max_nr_per_row as well from here?
    rows = math.ceil(l/max_tiles_per_row)
    while rows > 0:
        tiles_on_row = math.ceil(l/rows)
        grid.append(tiles_on_row)
        l -= tiles_on_row
        rows -= 1

    return grid


def input_event_loop(screen, tiles, active_tile, grid):
    t1 = time.time()
    running = True
    workspaces = len(global_knowledge) - 1

    while running and not global_updates_running and pygame.display.get_init():
        is_mouse_input = False
        kbdmove = None
        jump = False   # states whether we're navigating into a selected ws

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.MOUSEMOTION:
                is_mouse_input = True
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_UP or event.key == pygame.K_k:
                    kbdmove = (0, -1)
                elif event.key == pygame.K_DOWN or event.key == pygame.K_j:
                    kbdmove = (0, 1)
                elif event.key == pygame.K_LEFT or event.key == pygame.K_h:
                    kbdmove = (-1, 0)
                elif event.key == pygame.K_RIGHT or event.key == pygame.K_l:
                    kbdmove = (1, 0)
                elif event.key == pygame.K_RETURN:
                    jump = True
                elif event.key == pygame.K_ESCAPE:
                    running = False

                pygame.event.clear()
                break

            elif event.type == pygame.MOUSEBUTTONUP:
                is_mouse_input = True
                if event.button == 1:
                    jump = True
                pygame.event.clear()
                break

        # find the active/highlighted tile following mouse/keyboard navigation:
        if is_mouse_input and time.time() - t1 > 0.01:  # time comparison is so we don't register mouse event when win is first drawn;
            t = get_hovered_tile(pygame.mouse.get_pos(), tiles)
            if t is not None:
                active_tile = t
            else:
                jump = False  # make sure not to change workspace if we clicked between the tiles
        elif kbdmove is not None:
            if kbdmove[0] != 0:  # left-right movement
                active_tile += kbdmove[0]
                if active_tile > workspaces - 1:
                    active_tile = 0
                elif active_tile < 0:
                    active_tile = workspaces - 1
            elif len(grid) > 1:  # up-down movement
                current_row = tiles[active_tile]['row_idx']
                if current_row == 0:  # we're currently on first row
                    no_of_tiles_on_target_row = grid[kbdmove[1]]
                    prev_tiles = grid[0] if kbdmove[1] == 1 else sum(grid[:len(grid)-1])
                elif current_row == len(grid)-1:  # we're on last row
                    if kbdmove[1] == 1:
                        no_of_tiles_on_target_row = grid[0]
                        prev_tiles = 0
                    else:
                        no_of_tiles_on_target_row = grid[current_row-1]
                        prev_tiles = sum(grid[:len(grid)-2])
                else:
                    no_of_tiles_on_target_row = grid[current_row + kbdmove[1]]
                    prev_tiles = sum(grid[:current_row + kbdmove[1]])

                next_tiles = [i for i in range(prev_tiles, prev_tiles+no_of_tiles_on_target_row)]
                active_tile = get_new_active_tile(tiles, active_tile, next_tiles)

        if jump and active_tile is not None:
            target_ws_num = tiles[active_tile]['ws']
            i3.command('workspace ' + str(global_knowledge[target_ws_num]['name']))
            break

        draw_tile_overlays(screen, active_tile, tiles)

        if pygame.display.get_init():  # check as UI might've been closed by on_ws() from other thread
            pygame.display.update()
            pygame.time.wait(50)


def get_new_active_tile(tiles, active_tile, next_tiles):
    def get_center(t):
        return (t['br'][0] + t['ul'][0]) / 2  # given tile's centerpoint x-coord

    i = {}
    at_x = get_center(tiles[active_tile])
    for t in next_tiles:
        j = abs(at_x - get_center(tiles[t]))
        i[j] = t

    return i[min(list(i.keys()))]


# draw/reset highlight overlays:
def draw_tile_overlays(screen, active_tile, tiles):
    # first replace active thumbs with mouseoff/inactive ones, if tile is no longer active:
    for tile in tiles:
        if tile != active_tile and tiles[tile]['active']:
            screen.blit(tiles[tile]['mouseoff'], tiles[tile]['ul'])
            tiles[tile]['active'] = False
    # ...and finally paint mouseon/active thumb for an active/selected tile:
    if active_tile is not None and not tiles[active_tile]['active']:
        screen.blit(tiles[active_tile]['mouseon'], tiles[active_tile]['ul'])
        tiles[active_tile]['active'] = True


# TODO: is listening to 'workspace' event even necessary, as window::focus is fired anyway?
# only thing that it does is forcing immediate update, so it might be a good idea still...
def on_ws(i3, e):
    global global_updates_running

    global_updates_running = True  # make sure UI is closed on workspace switch

    # if e.change == 'rename':
        # pass  # TODO: handle ws rename event!
    # elif e.change == 'move':

    update_state(i3, e, rate_limit_period=loop_interval, force=True)


if __name__ == "__main__":  # pragma: no cover

    converters = {'color': get_color}
    config = configparser.ConfigParser(converters = converters)

    signal.signal(signal.SIGINT, signal_quit)
    signal.signal(signal.SIGTERM, signal_quit)
    signal.signal(signal.SIGHUP, signal_reload)
    signal.signal(signal.SIGUSR1, signal_toggle_ui)

    read_config()
    init_knowledge()
    updater_debounced = Debounce(config.getfloat('CONF', 'debounce_period_sec'),
                                 partial(update_state, debounced=True))
    update_state(i3, all_active_ws=True)

    # i3.on('window::new', update_state)  # no need when changing on window::focus
    # i3.on('window::close', update_state)  # no need when changing on window::focus
    i3.on('window::move', updater_debounced)
    i3.on('window::floating', updater_debounced)
    i3.on('window::fullscreen_mode', partial(updater_debounced, force=True))
    i3.on('window::focus', updater_debounced)
    i3.on('window::title', partial(updater_debounced, only_focused_win=True))
    i3.on('workspace', on_ws)
    i3.on('shutdown', on_shutdown)

    i3_thread = Thread(target = i3.main)
    i3_thread.daemon = True
    i3_thread.start()

    loop_interval = config.getfloat('CONF', 'forced_update_interval_sec')
    while True:
        time.sleep(loop_interval)
        # os.nice(10)
        update_state(i3, rate_limit_period=loop_interval, all_active_ws=True, force=True)
        # os.nice(-10)
