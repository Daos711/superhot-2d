import math
import random
import sys
from collections import deque

import pygame

# --- Config ---
WIDTH, HEIGHT = 1280, 720
FPS = 60

PLAYER_RADIUS = 14
PLAYER_SPEED = 280.0          # pixels per real second at full move
PLAYER_BULLET_SPEED = 900.0   # player bullets are NOT affected by time scale (player's own actions)
ENEMY_RADIUS = 16
ENEMY_SPEED = 90.0
ENEMY_FIRE_INTERVAL = 1.6     # seconds (in world time, so freezes when player stands still)
ENEMY_BULLET_SPEED = 360.0

# Time-scale tuning: how strongly player movement drives world time.
TS_MIN = 0.05                 # almost frozen when standing
TS_MAX = 1.0                  # full speed when sprinting
TS_LERP = 0.15                # smoothing factor toward target each frame
SPEED_WINDOW = 10             # frames of player speed to average

BG = (18, 18, 22)
WALL_COLOR = (90, 90, 96)
PLAYER_COLOR = (70, 140, 255)
PLAYER_AIM_COLOR = (200, 220, 255)
ENEMY_COLOR = (230, 70, 70)
PLAYER_BULLET_COLOR = (255, 240, 120)
ENEMY_BULLET_COLOR = (255, 120, 120)
HUD_COLOR = (220, 220, 220)


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def seg_rect_hit(x1, y1, x2, y2, rect):
    """Return True if segment (x1,y1)->(x2,y2) intersects rect. Used to stop bullets at walls."""
    # Liang-Barsky-ish. Quick reject + parametric clip.
    if rect.collidepoint(x1, y1) or rect.collidepoint(x2, y2):
        return True
    dx, dy = x2 - x1, y2 - y1
    p = [-dx, dx, -dy, dy]
    q = [x1 - rect.left, rect.right - x1, y1 - rect.top, rect.bottom - y1]
    u1, u2 = 0.0, 1.0
    for pi, qi in zip(p, q):
        if pi == 0:
            if qi < 0:
                return False
        else:
            t = qi / pi
            if pi < 0:
                if t > u2:
                    return False
                if t > u1:
                    u1 = t
            else:
                if t < u1:
                    return False
                if t < u2:
                    u2 = t
    return u1 <= u2


def move_circle_against_walls(x, y, r, dx, dy, walls):
    """Move a circle by (dx, dy), resolving against axis-aligned wall rects per-axis."""
    nx = x + dx
    for w in walls:
        # Closest point on rect to circle center
        cx = clamp(nx, w.left, w.right)
        cy = clamp(y, w.top, w.bottom)
        if (nx - cx) ** 2 + (y - cy) ** 2 < r * r:
            # Push out along x
            if dx > 0:
                nx = w.left - r - 0.01
            elif dx < 0:
                nx = w.right + r + 0.01
    ny = y + dy
    for w in walls:
        cx = clamp(nx, w.left, w.right)
        cy = clamp(ny, w.top, w.bottom)
        if (nx - cx) ** 2 + (ny - cy) ** 2 < r * r:
            if dy > 0:
                ny = w.top - r - 0.01
            elif dy < 0:
                ny = w.bottom + r + 0.01
    return nx, ny


def make_scene():
    walls = [
        pygame.Rect(300, 180, 220, 40),
        pygame.Rect(820, 200, 40, 240),
        pygame.Rect(420, 460, 280, 40),
        pygame.Rect(180, 520, 40, 160),
    ]
    enemies = []
    # Spawn 5 enemies near the edges
    edges = [
        (80, 80), (1180, 80), (1180, 620), (80, 620), (640, 60),
    ]
    for ex, ey in edges:
        enemies.append({
            "x": float(ex), "y": float(ey),
            "fire_cd": random.uniform(0.5, ENEMY_FIRE_INTERVAL),
            "alive": True,
        })
    return walls, enemies


def reset_state():
    walls, enemies = make_scene()
    return {
        "player": {"x": WIDTH * 0.5, "y": HEIGHT * 0.5, "alive": True},
        "walls": walls,
        "enemies": enemies,
        "p_bullets": [],   # player bullets: dicts with x,y,vx,vy
        "e_bullets": [],   # enemy bullets
        "speed_hist": deque([0.0] * SPEED_WINDOW, maxlen=SPEED_WINDOW),
        "time_scale": TS_MIN,
        "game_over": False,
    }


def main():
    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption("SUPERHOT 2D")
    clock = pygame.time.Clock()
    font = pygame.font.SysFont("consolas", 18)
    big_font = pygame.font.SysFont("consolas", 48, bold=True)

    state = reset_state()

    running = True
    while running:
        dt = clock.tick(FPS) / 1000.0  # real seconds since last frame

        # --- Input / events ---
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                running = False
            elif e.type == pygame.KEYDOWN:
                if e.key == pygame.K_ESCAPE:
                    running = False
                elif e.key == pygame.K_r:
                    state = reset_state()
            elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
                if state["player"]["alive"]:
                    px, py = state["player"]["x"], state["player"]["y"]
                    mx, my = pygame.mouse.get_pos()
                    dx, dy = mx - px, my - py
                    L = math.hypot(dx, dy) or 1.0
                    state["p_bullets"].append({
                        "x": px, "y": py,
                        "vx": dx / L * PLAYER_BULLET_SPEED,
                        "vy": dy / L * PLAYER_BULLET_SPEED,
                    })

        keys = pygame.key.get_pressed()
        player = state["player"]
        walls = state["walls"]

        # --- Player movement (NOT scaled by world time; player lives in real time) ---
        ix = (1 if keys[pygame.K_d] else 0) - (1 if keys[pygame.K_a] else 0)
        iy = (1 if keys[pygame.K_s] else 0) - (1 if keys[pygame.K_w] else 0)
        if ix or iy:
            L = math.hypot(ix, iy)
            ix, iy = ix / L, iy / L
        move_dx = ix * PLAYER_SPEED * dt
        move_dy = iy * PLAYER_SPEED * dt

        # Measure actual movement magnitude this frame to feed the time scale.
        # Using requested speed (not post-collision) so pushing into a wall still ticks time.
        if player["alive"]:
            player["x"], player["y"] = move_circle_against_walls(
                player["x"], player["y"], PLAYER_RADIUS, move_dx, move_dy, walls
            )
        # Keep player on screen
        player["x"] = clamp(player["x"], PLAYER_RADIUS, WIDTH - PLAYER_RADIUS)
        player["y"] = clamp(player["y"], PLAYER_RADIUS, HEIGHT - PLAYER_RADIUS)

        # --- Time scale calculation ---
        # Sample current normalized speed (0..1) and average it over the last SPEED_WINDOW frames.
        # That smoothing prevents one-frame spikes (e.g. tap of W) from snapping the world to full speed.
        cur_speed_norm = math.hypot(ix, iy)  # 0 when no input, 1 when moving
        state["speed_hist"].append(cur_speed_norm)
        avg = sum(state["speed_hist"]) / len(state["speed_hist"])
        target_ts = TS_MIN + (TS_MAX - TS_MIN) * avg
        # Lerp toward target so transitions feel smooth (no instant freeze/unfreeze).
        state["time_scale"] += (target_ts - state["time_scale"]) * TS_LERP
        ts = state["time_scale"]
        # World dt: every non-player thing uses this. Stand still -> world barely advances.
        wdt = dt * ts

        # --- Enemies AI (use wdt) ---
        for en in state["enemies"]:
            if not en["alive"]:
                continue
            dx, dy = player["x"] - en["x"], player["y"] - en["y"]
            L = math.hypot(dx, dy) or 1.0
            ndx, ndy = dx / L, dy / L
            mx = ndx * ENEMY_SPEED * wdt
            my = ndy * ENEMY_SPEED * wdt
            en["x"], en["y"] = move_circle_against_walls(
                en["x"], en["y"], ENEMY_RADIUS, mx, my, walls
            )
            en["fire_cd"] -= wdt
            if en["fire_cd"] <= 0 and player["alive"]:
                en["fire_cd"] = ENEMY_FIRE_INTERVAL
                state["e_bullets"].append({
                    "x": en["x"], "y": en["y"],
                    "vx": ndx * ENEMY_BULLET_SPEED,
                    "vy": ndy * ENEMY_BULLET_SPEED,
                })

        # --- Bullets ---
        # Player bullets fly in real time (player's own action), but feel snappy regardless of ts.
        new_pb = []
        for b in state["p_bullets"]:
            x0, y0 = b["x"], b["y"]
            b["x"] += b["vx"] * dt
            b["y"] += b["vy"] * dt
            if not (0 <= b["x"] <= WIDTH and 0 <= b["y"] <= HEIGHT):
                continue
            hit_wall = any(seg_rect_hit(x0, y0, b["x"], b["y"], w) for w in walls)
            if hit_wall:
                continue
            hit_enemy = False
            for en in state["enemies"]:
                if en["alive"] and (en["x"] - b["x"]) ** 2 + (en["y"] - b["y"]) ** 2 < ENEMY_RADIUS ** 2:
                    en["alive"] = False
                    hit_enemy = True
                    break
            if hit_enemy:
                continue
            new_pb.append(b)
        state["p_bullets"] = new_pb

        # Enemy bullets are scaled by world time -> nearly stop when player stops.
        new_eb = []
        for b in state["e_bullets"]:
            x0, y0 = b["x"], b["y"]
            b["x"] += b["vx"] * wdt
            b["y"] += b["vy"] * wdt
            if not (0 <= b["x"] <= WIDTH and 0 <= b["y"] <= HEIGHT):
                continue
            if any(seg_rect_hit(x0, y0, b["x"], b["y"], w) for w in walls):
                continue
            if player["alive"]:
                if (player["x"] - b["x"]) ** 2 + (player["y"] - b["y"]) ** 2 < (PLAYER_RADIUS + 2) ** 2:
                    player["alive"] = False
                    state["game_over"] = True
                    continue
            new_eb.append(b)
        state["e_bullets"] = new_eb

        if all(not en["alive"] for en in state["enemies"]):
            state["game_over"] = True  # win

        # --- Draw ---
        screen.fill(BG)
        for w in walls:
            pygame.draw.rect(screen, WALL_COLOR, w)

        for en in state["enemies"]:
            if en["alive"]:
                pygame.draw.circle(screen, ENEMY_COLOR, (int(en["x"]), int(en["y"])), ENEMY_RADIUS)

        for b in state["e_bullets"]:
            # Short line tail in direction of motion makes them readable even when nearly frozen.
            tail = 6
            L = math.hypot(b["vx"], b["vy"]) or 1.0
            tx = b["x"] - b["vx"] / L * tail
            ty = b["y"] - b["vy"] / L * tail
            pygame.draw.line(screen, ENEMY_BULLET_COLOR, (tx, ty), (b["x"], b["y"]), 3)

        for b in state["p_bullets"]:
            pygame.draw.circle(screen, PLAYER_BULLET_COLOR, (int(b["x"]), int(b["y"])), 3)

        if player["alive"]:
            pygame.draw.circle(screen, PLAYER_COLOR, (int(player["x"]), int(player["y"])), PLAYER_RADIUS)
            mx, my = pygame.mouse.get_pos()
            dxa, dya = mx - player["x"], my - player["y"]
            La = math.hypot(dxa, dya) or 1.0
            ax = player["x"] + dxa / La * (PLAYER_RADIUS + 18)
            ay = player["y"] + dya / La * (PLAYER_RADIUS + 18)
            pygame.draw.line(screen, PLAYER_AIM_COLOR,
                             (player["x"], player["y"]), (ax, ay), 2)
            pygame.draw.circle(screen, PLAYER_AIM_COLOR, (mx, my), 4, 1)

        # HUD
        hud = font.render(f"time x{ts:0.2f}   WASD move | LMB shoot | R restart | ESC quit", True, HUD_COLOR)
        screen.blit(hud, (10, 10))

        if state["game_over"]:
            won = all(not en["alive"] for en in state["enemies"]) and player["alive"]
            msg = "YOU WIN" if won else "YOU DIED"
            text = big_font.render(msg, True, (255, 255, 255))
            sub = font.render("Press R to restart", True, HUD_COLOR)
            screen.blit(text, text.get_rect(center=(WIDTH // 2, HEIGHT // 2 - 20)))
            screen.blit(sub, sub.get_rect(center=(WIDTH // 2, HEIGHT // 2 + 30)))

        pygame.display.flip()

    pygame.quit()
    sys.exit(0)


if __name__ == "__main__":
    main()
