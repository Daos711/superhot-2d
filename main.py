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
PLAYER_BULLET_SPEED = 900.0   # speed at full world time; bullets are scaled by ts like everything else in the world
PLAYER_FIRE_INTERVAL = 0.25   # world-time seconds between player shots — freezes when player stops
ENEMY_RADIUS = 16

# Time-scale tuning: how strongly player movement drives world time.
TS_MIN = 0.05                 # almost frozen when standing
TS_MAX = 1.0                  # full speed when sprinting
TS_LERP = 0.15                # smoothing factor toward target each frame
SPEED_WINDOW = 10             # frames of player speed to average

BG = (18, 18, 22)
WALL_COLOR = (90, 90, 96)
PLAYER_COLOR = (70, 140, 255)
PLAYER_AIM_COLOR = (200, 220, 255)
PLAYER_BULLET_COLOR = (255, 240, 120)
ENEMY_BULLET_COLOR = (255, 120, 120)
HUD_COLOR = (220, 220, 220)

# Per-type enemy stats. Everything time-related (fire_interval, telegraph)
# is consumed in world (scaled) seconds, so it freezes when player stops.
ENEMY_KINDS = {
    "shooter": {
        "color": (230, 70, 70),
        "speed": 115.0,
        "fire_interval": 1.4,     # gap between bursts (world seconds)
        "burst_size": 2,          # shots per burst
        "burst_gap": 0.22,        # short cooldown between shots inside a burst
        "bullet_speed": 410.0,
        "preferred_dist": 260.0,
        "lead": True,
    },
    "runner": {
        "color": (255, 150, 50),
        "speed": 200.0,           # ~1.7x of shooter
        "fire_interval": 0.0,
        "bullet_speed": 0.0,
        "preferred_dist": 0.0,
        "lead": False,
    },
    "sniper": {
        "color": (150, 30, 55),
        "speed": 55.0,
        "fire_interval": 3.0,
        "burst_size": 1,
        "burst_gap": 0.0,
        "bullet_speed": 760.0,
        "preferred_dist": 430.0,
        "telegraph_time": 0.55,   # world seconds the red beam is visible before firing
        "lead": True,
    },
}


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def seg_rect_hit(x1, y1, x2, y2, rect):
    """Return True if segment (x1,y1)->(x2,y2) intersects rect. Used for bullets and LoS."""
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


def has_los(x1, y1, x2, y2, walls):
    """Line of sight: no wall blocks the segment."""
    return not any(seg_rect_hit(x1, y1, x2, y2, w) for w in walls)


def ray_first_wall(ox, oy, dx, dy, walls, max_t):
    """
    Distance along unit ray (ox,oy)+t*(dx,dy) until it first enters a wall, or max_t.
    Used to draw the sniper telegraph beam: it stops at whatever the bullet would hit,
    so the player sees the actual bullet path instead of a line to a hidden lead point.
    """
    best = max_t
    for w in walls:
        t_min, t_max = 0.0, best
        ok = True
        for axis in (0, 1):
            o = ox if axis == 0 else oy
            d = dx if axis == 0 else dy
            lo = w.left if axis == 0 else w.top
            hi = w.right if axis == 0 else w.bottom
            if abs(d) < 1e-9:
                if o < lo or o > hi:
                    ok = False
                    break
                continue
            t1 = (lo - o) / d
            t2 = (hi - o) / d
            if t1 > t2:
                t1, t2 = t2, t1
            if t1 > t_min:
                t_min = t1
            if t2 < t_max:
                t_max = t2
            if t_min > t_max:
                ok = False
                break
        if ok and 0 < t_min < best:
            best = t_min
    return best


def move_circle_against_walls(x, y, r, dx, dy, walls):
    """Move a circle by (dx, dy), resolving against axis-aligned wall rects per-axis."""
    nx = x + dx
    for w in walls:
        cx = clamp(nx, w.left, w.right)
        cy = clamp(y, w.top, w.bottom)
        if (nx - cx) ** 2 + (y - cy) ** 2 < r * r:
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


def steer_toward(en, tx, ty, walls):
    """
    Return a unit direction (mvx, mvy) for an enemy heading to (tx, ty).
    If a wall blocks the straight line, slide perpendicular (sample both sides,
    pick the one that yields LoS to the target). Lightweight, no real pathfinding.
    """
    dx, dy = tx - en["x"], ty - en["y"]
    L = math.hypot(dx, dy) or 1.0
    ndx, ndy = dx / L, dy / L
    if has_los(en["x"], en["y"], tx, ty, walls):
        return ndx, ndy

    perp_x, perp_y = -ndy, ndx
    probe = 70.0
    # Try preferred slide side first, then opposite. Lock in whichever clears LoS.
    for sign in (en["slide"], -en["slide"]):
        px = en["x"] + perp_x * sign * probe
        py = en["y"] + perp_y * sign * probe
        if has_los(px, py, tx, ty, walls):
            en["slide"] = sign
            mx = perp_x * sign * 0.85 + ndx * 0.15
            my = perp_y * sign * 0.85 + ndy * 0.15
            ml = math.hypot(mx, my) or 1.0
            return mx / ml, my / ml
    # Both blocked — keep current slide bias and hope to round the corner next frame.
    sign = en["slide"]
    mx = perp_x * sign * 0.85 + ndx * 0.15
    my = perp_y * sign * 0.85 + ndy * 0.15
    ml = math.hypot(mx, my) or 1.0
    return mx / ml, my / ml


def predict_target(ex, ey, px, py, pvx, pvy, bullet_speed):
    """Naive lead: assume player keeps current velocity for travel-time of the bullet."""
    dx, dy = px - ex, py - ey
    dist = math.hypot(dx, dy)
    t = dist / bullet_speed if bullet_speed > 0 else 0.0
    return px + pvx * t, py + pvy * t


def fire_at(en, tx, ty, speed, e_bullets):
    dx, dy = tx - en["x"], ty - en["y"]
    L = math.hypot(dx, dy) or 1.0
    e_bullets.append({
        "x": en["x"], "y": en["y"],
        "vx": dx / L * speed,
        "vy": dy / L * speed,
    })


def make_scene():
    walls = [
        pygame.Rect(300, 180, 220, 40),
        pygame.Rect(820, 200, 40, 240),
        pygame.Rect(420, 460, 280, 40),
        pygame.Rect(180, 520, 40, 160),
    ]
    # 3 shooters + 3 runners + 2 snipers — covers angles, makes simple stand-and-fire
    # impossible: shooters orbit, runners crash in, snipers cover the sides.
    spec = [
        ("shooter", 100,  100),
        ("shooter", 1180, 620),
        ("shooter", 100,  620),
        ("runner",  1180, 100),
        ("runner",  640,  60),
        ("runner",  640,  660),
        ("sniper",  60,   360),
        ("sniper",  1220, 360),
    ]
    enemies = []
    for kind, x, y in spec:
        cfg = ENEMY_KINDS[kind]
        enemies.append({
            "kind": kind,
            "x": float(x), "y": float(y),
            "fire_cd": random.uniform(0.4, max(0.5, cfg["fire_interval"])),
            "burst_left": cfg.get("burst_size", 1),
            "aim_t": 0.0,            # >0 means sniper is in telegraph phase
            "aim_x": float(x), "aim_y": float(y),
            "slide": random.choice((-1, 1)),
            "strafe": random.choice((-1, 1)),
            "strafe_timer": random.uniform(1.5, 3.5),
            "alive": True,
        })
    return walls, enemies


def reset_state():
    walls, enemies = make_scene()
    return {
        "player": {"x": WIDTH * 0.5, "y": HEIGHT * 0.5, "vx": 0.0, "vy": 0.0, "alive": True},
        "walls": walls,
        "enemies": enemies,
        "p_bullets": [],
        "e_bullets": [],
        "speed_hist": deque([0.0] * SPEED_WINDOW, maxlen=SPEED_WINDOW),
        "time_scale": TS_MIN,
        "fire_cd": 0.0,
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
        dt = clock.tick(FPS) / 1000.0

        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                running = False
            elif e.type == pygame.KEYDOWN:
                if e.key == pygame.K_ESCAPE:
                    running = False
                elif e.key == pygame.K_r:
                    state = reset_state()
            elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
                # Fire cooldown is in WORLD time — standing still freezes it, so you
                # get exactly one shot per "stop", forcing the move-shoot-move rhythm.
                if state["player"]["alive"] and state["fire_cd"] <= 0:
                    px, py = state["player"]["x"], state["player"]["y"]
                    mx, my = pygame.mouse.get_pos()
                    dx, dy = mx - px, my - py
                    L = math.hypot(dx, dy) or 1.0
                    state["p_bullets"].append({
                        "x": px, "y": py,
                        "vx": dx / L * PLAYER_BULLET_SPEED,
                        "vy": dy / L * PLAYER_BULLET_SPEED,
                    })
                    state["fire_cd"] = PLAYER_FIRE_INTERVAL

        keys = pygame.key.get_pressed()
        player = state["player"]
        walls = state["walls"]

        # --- Player movement (real time, NOT scaled) ---
        ix = (1 if keys[pygame.K_d] else 0) - (1 if keys[pygame.K_a] else 0)
        iy = (1 if keys[pygame.K_s] else 0) - (1 if keys[pygame.K_w] else 0)
        if ix or iy:
            L = math.hypot(ix, iy)
            ix, iy = ix / L, iy / L
        prev_x, prev_y = player["x"], player["y"]
        if player["alive"]:
            player["x"], player["y"] = move_circle_against_walls(
                player["x"], player["y"], PLAYER_RADIUS,
                ix * PLAYER_SPEED * dt, iy * PLAYER_SPEED * dt, walls
            )
        player["x"] = clamp(player["x"], PLAYER_RADIUS, WIDTH - PLAYER_RADIUS)
        player["y"] = clamp(player["y"], PLAYER_RADIUS, HEIGHT - PLAYER_RADIUS)
        # Real (post-collision) velocity, used to lead shots.
        player["vx"] = (player["x"] - prev_x) / dt if dt > 0 else 0.0
        player["vy"] = (player["y"] - prev_y) / dt if dt > 0 else 0.0

        # --- Time scale calculation ---
        # Sample current normalized speed (0..1) and average over SPEED_WINDOW frames.
        # Smoothing prevents one-frame taps from snapping the world to full speed.
        cur_speed_norm = math.hypot(ix, iy)
        state["speed_hist"].append(cur_speed_norm)
        avg = sum(state["speed_hist"]) / len(state["speed_hist"])
        target_ts = TS_MIN + (TS_MAX - TS_MIN) * avg
        state["time_scale"] += (target_ts - state["time_scale"]) * TS_LERP
        ts = state["time_scale"]
        # World dt: every non-player thing (movement, cooldowns, telegraphs, bullets) uses this.
        wdt = dt * ts

        # Player fire cooldown ticks proportional to actual movement, not to wdt.
        # If we used wdt, the TS_MIN floor (0.05) would slowly drain the cooldown
        # while standing still — letting the player fire again every ~5s without
        # ever moving. Tying it to avg movement makes a full stop a hard freeze.
        if state["fire_cd"] > 0:
            state["fire_cd"] -= dt * avg

        # --- Enemy AI ---
        for en in state["enemies"]:
            if not en["alive"]:
                continue
            kind = en["kind"]
            cfg = ENEMY_KINDS[kind]
            dx = player["x"] - en["x"]
            dy = player["y"] - en["y"]
            dist = math.hypot(dx, dy) or 1.0
            los = has_los(en["x"], en["y"], player["x"], player["y"], walls)

            # Decide where to walk this frame.
            mvx = mvy = 0.0
            if kind == "runner":
                mvx, mvy = steer_toward(en, player["x"], player["y"], walls)
            elif kind == "shooter":
                # Orbit at preferred_dist: target sits on a circle around the player,
                # offset by ~35 deg in current strafe direction. Periodic flips of
                # strafe sign keep movement unpredictable so the player can't pre-aim.
                ideal = cfg["preferred_dist"]
                cur_angle = math.atan2(en["y"] - player["y"], en["x"] - player["x"])
                strafe_angle = cur_angle + en["strafe"] * 0.6
                tx = player["x"] + math.cos(strafe_angle) * ideal
                ty = player["y"] + math.sin(strafe_angle) * ideal
                tx = clamp(tx, ENEMY_RADIUS + 6, WIDTH - ENEMY_RADIUS - 6)
                ty = clamp(ty, ENEMY_RADIUS + 6, HEIGHT - ENEMY_RADIUS - 6)
                mvx, mvy = steer_toward(en, tx, ty, walls)
                en["strafe_timer"] -= wdt
                if en["strafe_timer"] <= 0:
                    en["strafe"] *= -1
                    en["strafe_timer"] = random.uniform(1.5, 3.5)
            elif kind == "sniper":
                ideal = cfg["preferred_dist"]
                if not los or dist > ideal + 60:
                    mvx, mvy = steer_toward(en, player["x"], player["y"], walls)
                elif dist < ideal - 100:
                    # Back away, but clamp the target into the playable area so the
                    # sniper doesn't try to walk off the screen edge.
                    bx = clamp(en["x"] - dx / dist * 200, ENEMY_RADIUS + 6, WIDTH - ENEMY_RADIUS - 6)
                    by = clamp(en["y"] - dy / dist * 200, ENEMY_RADIUS + 6, HEIGHT - ENEMY_RADIUS - 6)
                    mvx, mvy = steer_toward(en, bx, by, walls)

            if mvx or mvy:
                speed = cfg["speed"]
                en["x"], en["y"] = move_circle_against_walls(
                    en["x"], en["y"], ENEMY_RADIUS,
                    mvx * speed * wdt, mvy * speed * wdt, walls
                )
            # Hard clamp to screen — without this, anything aimed at a target near
            # the edge can creep out (the wall collider doesn't know about screen bounds).
            en["x"] = clamp(en["x"], ENEMY_RADIUS, WIDTH - ENEMY_RADIUS)
            en["y"] = clamp(en["y"], ENEMY_RADIUS, HEIGHT - ENEMY_RADIUS)

            # --- Combat per kind ---
            if kind == "runner":
                # Contact damage. Note: runner moves on wdt, so if player is still,
                # the runner is frozen — the only way to die to a runner is to walk into one.
                if (en["x"] - player["x"]) ** 2 + (en["y"] - player["y"]) ** 2 \
                        < (ENEMY_RADIUS + PLAYER_RADIUS) ** 2:
                    if player["alive"]:
                        player["alive"] = False
                        state["game_over"] = True
            elif kind == "sniper":
                # Continuously update the predicted intercept while alive — the telegraph
                # line tracks where the shot will actually go.
                if cfg["lead"] and player["alive"]:
                    en["aim_x"], en["aim_y"] = predict_target(
                        en["x"], en["y"], player["x"], player["y"],
                        player["vx"], player["vy"], cfg["bullet_speed"]
                    )
                else:
                    en["aim_x"], en["aim_y"] = player["x"], player["y"]

                if en["aim_t"] > 0:
                    # Telegraphing — count down in WORLD time, freezes when player stops.
                    en["aim_t"] -= wdt
                    if not los:
                        en["aim_t"] = 0.0  # cancel if cover is taken during telegraph
                    elif en["aim_t"] <= 0 and player["alive"]:
                        fire_at(en, en["aim_x"], en["aim_y"], cfg["bullet_speed"], state["e_bullets"])
                        en["fire_cd"] = cfg["fire_interval"]
                        en["aim_t"] = 0.0
                else:
                    en["fire_cd"] -= wdt
                    if en["fire_cd"] <= 0 and los and player["alive"]:
                        en["aim_t"] = cfg["telegraph_time"]
            else:  # shooter — fires bursts of cfg["burst_size"] shots
                en["fire_cd"] -= wdt
                if en["fire_cd"] <= 0 and los and player["alive"]:
                    if cfg["lead"]:
                        tx, ty = predict_target(
                            en["x"], en["y"], player["x"], player["y"],
                            player["vx"], player["vy"], cfg["bullet_speed"]
                        )
                    else:
                        tx, ty = player["x"], player["y"]
                    fire_at(en, tx, ty, cfg["bullet_speed"], state["e_bullets"])
                    en["burst_left"] -= 1
                    if en["burst_left"] > 0:
                        en["fire_cd"] = cfg["burst_gap"]
                    else:
                        en["fire_cd"] = cfg["fire_interval"]
                        en["burst_left"] = cfg["burst_size"]

        # --- Player bullets (world time) ---
        # Once a bullet is in the air it belongs to the world, so it slows down with
        # everything else when the player stops. This is the core SUPERHOT loop:
        # you can't pre-fire from cover and then sit still — to actually hit anything
        # you have to commit to motion, which also wakes the enemies up.
        new_pb = []
        for b in state["p_bullets"]:
            x0, y0 = b["x"], b["y"]
            b["x"] += b["vx"] * wdt
            b["y"] += b["vy"] * wdt
            if not (0 <= b["x"] <= WIDTH and 0 <= b["y"] <= HEIGHT):
                continue
            if any(seg_rect_hit(x0, y0, b["x"], b["y"], w) for w in walls):
                continue
            hit = False
            for en in state["enemies"]:
                if en["alive"] and (en["x"] - b["x"]) ** 2 + (en["y"] - b["y"]) ** 2 < ENEMY_RADIUS ** 2:
                    en["alive"] = False
                    hit = True
                    break
            if hit:
                continue
            new_pb.append(b)
        state["p_bullets"] = new_pb

        # --- Enemy bullets (world/scaled time) ---
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
            state["game_over"] = True

        # --- Draw ---
        screen.fill(BG)
        for w in walls:
            pygame.draw.rect(screen, WALL_COLOR, w)

        # Sniper telegraph beams: traced as the actual bullet ray (clipped at the first
        # wall hit) so the player sees exactly where the shot will fly, regardless of
        # where the predicted lead point actually sits.
        for en in state["enemies"]:
            if not en["alive"] or en["kind"] != "sniper" or en["aim_t"] <= 0:
                continue
            cfg = ENEMY_KINDS["sniper"]
            ddx = en["aim_x"] - en["x"]
            ddy = en["aim_y"] - en["y"]
            L = math.hypot(ddx, ddy) or 1.0
            ndx, ndy = ddx / L, ddy / L
            t_end = ray_first_wall(en["x"], en["y"], ndx, ndy, walls, 2000.0)
            ex = en["x"] + ndx * t_end
            ey = en["y"] + ndy * t_end
            progress = 1.0 - en["aim_t"] / cfg["telegraph_time"]
            r = int(110 + 145 * progress)
            g = int(20 + 40 * progress)
            b_ = int(40 + 60 * progress)
            thick = 1 + int(progress * 2)
            pygame.draw.line(screen, (r, g, b_), (en["x"], en["y"]), (ex, ey), thick)

        for en in state["enemies"]:
            if not en["alive"]:
                continue
            cfg = ENEMY_KINDS[en["kind"]]
            pygame.draw.circle(screen, cfg["color"], (int(en["x"]), int(en["y"])), ENEMY_RADIUS)
            # Small inner mark so types are readable: runner has a ring, sniper a dot.
            if en["kind"] == "runner":
                pygame.draw.circle(screen, (40, 25, 10), (int(en["x"]), int(en["y"])), 5)
            elif en["kind"] == "sniper":
                pygame.draw.circle(screen, (240, 220, 220), (int(en["x"]), int(en["y"])), 3)

        for b in state["e_bullets"]:
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
            # Dim the aim while reloading so the player can read fire-readiness at a glance.
            ready = state["fire_cd"] <= 0
            aim_col = PLAYER_AIM_COLOR if ready else (110, 120, 140)
            pygame.draw.line(screen, aim_col,
                             (player["x"], player["y"]), (ax, ay), 2)
            pygame.draw.circle(screen, aim_col, (mx, my), 4, 1)
            # Cooldown arc: a faint ring that sweeps closed as the cooldown ticks down.
            if not ready:
                frac = clamp(state["fire_cd"] / PLAYER_FIRE_INTERVAL, 0.0, 1.0)
                # Arc spans (1-frac) of the full circle to indicate progress.
                rect = pygame.Rect(0, 0, PLAYER_RADIUS * 2 + 8, PLAYER_RADIUS * 2 + 8)
                rect.center = (int(player["x"]), int(player["y"]))
                pygame.draw.arc(screen, (180, 200, 230), rect,
                                -math.pi / 2, -math.pi / 2 + (1 - frac) * 2 * math.pi, 2)

        hud = font.render(
            f"time x{ts:0.2f}   WASD move | LMB shoot | R restart | ESC quit",
            True, HUD_COLOR
        )
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
