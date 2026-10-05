import sys
sys.stdout.reconfigure(encoding='utf-8')

with open('avatar.html', 'r', encoding='utf-8') as f:
    content = f.read()

GESTURE_START = 49775
ANIMATE_END   = 66581

NEW_BLOCK = r"""        // ══════════════════════════════════════════════════════════════
        //  SPEECH GESTURE SYSTEM — Gestos diferenciados por personalidad
        //  Nino (tsundere): gestos asertivos, brazos cruzados, señalar
        //  Miku  (idol):    gestos abiertos, rítmicos, entusiastas
        // ══════════════════════════════════════════════════════════════
        const gestureSystem = {
            idx: 8,       // 8 = idle relajado (posición neutra por defecto)
            timer: 0,
            interval: 2.8,

            // ── Gestos de NINO (tsundere) ── índices 0–4
            gesturesNino: [
                // 0: Explica con autoridad — señala con el dedo derecho levantado
                (t, s) => ({
                    LUA:{z:1.30+Math.sin(t*1.5)*0.02, x:0.01},
                    RUA:{z:-(0.62+Math.sin(t*1.8)*0.06*s), x:-0.22*s},
                    LLA:{z:0.04, x:Math.sin(t*1.7)*0.03*s},
                    RLA:{z:-0.18*s, x:-0.32*s}
                }),
                // 1: Tsundere — voltea la cabeza, brazo cruzado
                (t, s) => ({
                    LUA:{z:0.72+Math.sin(t*1.1)*0.03, x:0.38*s},
                    RUA:{z:-(0.72+Math.sin(t*1.1)*0.03), x:-0.38*s},
                    LLA:{z:0.22*s, x:0.12*s},
                    RLA:{z:-0.22*s, x:-0.12*s}
                }),
                // 2: Pensativa — mano apoya la barbilla, pose reflexiva
                (t, s) => ({
                    LUA:{z:1.28+Math.sin(t*0.9)*0.02, x:0},
                    RUA:{z:-(0.38+Math.sin(t*1.2)*0.025*s), x:-0.42*s},
                    LLA:{z:0.02, x:0},
                    RLA:{z:0.05*s, x:-0.55*s}
                }),
                // 3: Énfasis fuerte — corta el aire con la mano derecha
                (t, s) => ({
                    LUA:{z:1.22+Math.sin(t*1.4)*0.03, x:0.04*s},
                    RUA:{z:-(0.58+Math.sin(t*3.2)*0.12*s), x:-0.18*s},
                    LLA:{z:0.06, x:Math.sin(t*3.2)*0.08*s},
                    RLA:{z:-0.14*s, x:-0.24*s}
                }),
                // 4: Orgullosa — manos en caderas (pequeña variación)
                (t, s) => ({
                    LUA:{z:0.88+Math.sin(t*0.8)*0.02, x:0.28*s},
                    RUA:{z:-(0.88+Math.sin(t*0.8)*0.02), x:-0.28*s},
                    LLA:{z:0.30*s, x:0.18*s},
                    RLA:{z:-0.30*s, x:-0.18*s}
                }),
            ],

            // ── Gestos de MIKU (idol alegre) ── índices 5–9
            gesturesMiku: [
                // 5: Entusiasta — ambas manos arriba con bounce
                (t, s) => ({
                    LUA:{z:0.58+Math.sin(t*3.8+0.3)*0.18*s, x:0.14*s},
                    RUA:{z:-(0.58+Math.sin(t*3.8)*0.18*s), x:-0.14*s},
                    LLA:{z:0.12*s, x:Math.sin(t*4.0)*0.10*s},
                    RLA:{z:-0.12*s, x:Math.sin(t*4.0+0.5)*0.10*s}
                }),
                // 6: Melodiosa — manos dibujan ondas en el aire
                (t, s) => ({
                    LUA:{z:0.82+Math.sin(t*2.2)*0.14*s, x:0.12*s},
                    RUA:{z:-(0.82+Math.sin(t*2.2+1.2)*0.14*s), x:-0.12*s},
                    LLA:{z:0.18*s, x:Math.cos(t*2.8)*0.12*s},
                    RLA:{z:-0.18*s, x:Math.cos(t*2.8+0.9)*0.12*s}
                }),
                // 7: Alegre — rebota suavemente
                (t, s) => ({
                    LUA:{z:1.02+Math.sin(t*4.2)*0.16*s, x:Math.sin(t*2.5)*0.09*s},
                    RUA:{z:-(1.02+Math.sin(t*4.2+1.0)*0.16*s), x:-Math.sin(t*2.5)*0.09*s},
                    LLA:{z:0.08*s, x:Math.sin(t*4.5)*0.08*s},
                    RLA:{z:-0.08*s, x:Math.sin(t*4.5+0.6)*0.08*s}
                }),
                // 8: Wink/coqueta — una mano sube hacia el ojo
                (t, s) => ({
                    LUA:{z:1.30+Math.sin(t*1.0)*0.02, x:0.02},
                    RUA:{z:-(0.44+Math.sin(t*1.4)*0.04*s), x:-0.32*s},
                    LLA:{z:0.04, x:0},
                    RLA:{z:0.04*s, x:-0.42*s}
                }),
                // 9: Presenta — brazos abiertos invitando al público
                (t, s) => ({
                    LUA:{z:0.68+Math.sin(t*1.5)*0.08*s, x:0.16*s},
                    RUA:{z:-(0.68+Math.sin(t*1.5+0.4)*0.08*s), x:-0.16*s},
                    LLA:{z:0.22*s, x:Math.sin(t*2.0)*0.07*s},
                    RLA:{z:-0.22*s, x:Math.sin(t*2.0+0.8)*0.07*s}
                }),
            ],

            // ── Idle neutro (8): brazos caídos, respiración sutil ──
            idlePose(t) {
                return {
                    LUA:{z:1.42+Math.sin(t*1.4)*0.014, x:0.018+Math.sin(t*0.85)*0.009},
                    RUA:{z:-1.42-Math.sin(t*1.4)*0.014, x:-0.018-Math.sin(t*0.85)*0.009},
                    LLA:{z:0.05+Math.sin(t*1.55+0.6)*0.018, x:0},
                    RLA:{z:-0.05-Math.sin(t*1.55+0.6)*0.018, x:0}
                };
            },

            update(vrm, delta, time, speaking, waifuName) {
                if (!vrm || !vrm.humanoid) return;
                const isNino = waifuName === 'nino';
                const pool = isNino ? this.gesturesNino : this.gesturesMiku;
                const poolSize = pool.length;

                // Mientras habla: rota gestos cada 2-4 s con variación de personalidad
                if (speaking > 0.1) {
                    this.timer += delta;
                    const changeInterval = isNino
                        ? 2.2 + Math.random() * 2.4   // Nino: gestos más frecuentes y asertivos
                        : 1.8 + Math.random() * 2.8;  // Miku: más variada, cambia más rápido
                    if (this.timer > this.interval) {
                        this.timer = 0;
                        this.interval = changeInterval;
                        let next = this.idx;
                        while (next === this.idx) next = Math.floor(Math.random() * poolSize);
                        this.idx = next;
                    }
                } else {
                    this.idx = poolSize; // idle
                    this.timer = 0;
                }

                // Elegir pose: idle o gesto activo
                const g = this.idx >= poolSize
                    ? this.idlePose(time)
                    : pool[this.idx](time, speaking);

                const sign = vrm._isVRM1 ? -1 : 1;
                const bones = {
                    LUA: vrm.humanoid.getNormalizedBoneNode('leftUpperArm'),
                    RUA: vrm.humanoid.getNormalizedBoneNode('rightUpperArm'),
                    LLA: vrm.humanoid.getNormalizedBoneNode('leftLowerArm'),
                    RLA: vrm.humanoid.getNormalizedBoneNode('rightLowerArm')
                };

                // Interpolación más suave para Miku (más fluida), más directa para Nino
                const blendSpeed = isNino ? 3.2 : 4.0;
                const sp  = Math.min(1, delta * blendSpeed);
                const spF = Math.min(1, delta * (blendSpeed + 0.8));

                for (const [key, bone] of Object.entries(bones)) {
                    if (!bone || !g[key]) continue;
                    const targetZ = g[key].z * sign;
                    bone.rotation.z += (targetZ - bone.rotation.z) * sp;
                    bone.rotation.x += (g[key].x - bone.rotation.x) * spF;
                }
            },
            reset() { this.idx = 8; this.timer = 0; }
        };

        // ══════════════════════════════════════════════════════════════
        //  IDLE RESTLESS SYSTEM — Inquietud diferenciada por personalidad
        //  Nino:  se impacienta, se cruza de brazos, bufidos sutiles
        //  Miku:  balancea suavemente, mira alrededor con curiosidad
        // ══════════════════════════════════════════════════════════════
        const idleRestless = {
            lastInteraction: Date.now(),
            phase: 0,
            actionTimer: 0,
            actionInterval: 5.5,
            action: 'none',
            actionProgress: 0,

            notify() { this.lastInteraction = Date.now(); this.phase = 0; },

            update(delta, isSpeaking, waifuName) {
                if (isSpeaking) { this.lastInteraction = Date.now(); return { rs: 0, action: 'none', ap: 0, phase: 0 }; }
                const elapsed = (Date.now() - this.lastInteraction) / 1000;
                let rs = 0;

                if (elapsed > 30) { rs = Math.min(1, (elapsed - 30) / 10) * 0.9; this.phase = 2; }
                else if (elapsed > 15) { rs = Math.min(1, (elapsed - 15) / 8) * 0.7; this.phase = 1; }
                else this.phase = 0;

                this.actionTimer += delta;
                if (this.actionTimer > this.actionInterval && this.phase >= 1) {
                    this.actionTimer = 0;
                    this.actionInterval = 3.2 + Math.random() * 5.0;

                    // Nino se impacienta → acciones más tsundere
                    // Miku se distrae → acciones más curiosas y suaves
                    const ninoActions = ['hairTouch', 'armsCross', 'lookAround', 'sigh', 'fidget'];
                    const mikuActions = ['hairTouch', 'lookAround', 'sway', 'sigh', 'fidget'];
                    const pool = waifuName === 'nino' ? ninoActions : mikuActions;
                    this.action = pool[Math.floor(Math.random() * pool.length)];
                    this.actionProgress = 0;
                }
                if (this.action !== 'none') {
                    this.actionProgress += delta;
                    if (this.actionProgress > 2.8) { this.action = 'none'; this.actionProgress = 0; }
                }
                return { rs, action: this.action, ap: this.actionProgress, phase: this.phase };
            }
        };

        // ── Estado global ──
        let isSpeaking    = false;
        let speakingSmooth = 0;
        let isListening   = false;
        let listenSmooth  = 0;

        window.setNinoSpeaking = function(val) {
            isSpeaking = val;
            if (val) idleRestless.notify();
            const dot = document.getElementById('status-dot');
            if (dot) { val ? dot.classList.add('speaking') : dot.classList.remove('speaking'); }
        };
        window.setNinoListening = function(val) {
            isListening = val;
            if (val) idleRestless.notify();
        };

        window.addEventListener('resize', () => {
            camera.aspect = window.innerWidth / window.innerHeight;
            camera.updateProjectionMatrix();
            renderer.setSize(window.innerWidth, window.innerHeight);
        });

        // ══════════════════════════════════════════════════════════════
        //  MAIN ANIMATION LOOP — con lógica de personalidad por waifu
        // ══════════════════════════════════════════════════════════════
        const clock = new THREE.Clock();
        // Parpadeo
        let blinkTimer = 0, nextBlink = 3.2, isBlinking = false, isDouble = false, doubleStage = 0;
        // Microexpresiones
        let microTimer = 0, nextMicro = 4 + Math.random() * 5, microExpr = 'none', microProg = 0;

        function animate() {
            requestAnimationFrame(animate);
            const delta = Math.min(clock.getDelta(), 0.05);
            const time  = clock.getElapsedTime();
            controls.update();

            if (!currentVrm) { renderer.render(scene, camera); return; }

            try {
                const waifu   = activeWaifuAnim;
                const isNino  = waifu === 'nino';
                const isMiku  = waifu === 'miku';

                // ── Entrada caminando + bounce de Miku ──
                const isEntering = walkEntrance.update(currentVrm, delta);
                if (!isEntering) walkEntrance.updateBounce(currentVrm, delta);

                // ── Smooth signals ──
                speakingSmooth += ((isSpeaking ? 1 : 0) - speakingSmooth) * Math.min(1, delta * 10);
                listenSmooth   += ((isListening ? 1 : 0) - listenSmooth)  * Math.min(1, delta * 8);
                const S = speakingSmooth;
                const L = listenSmooth;

                // ── Idle restless ──
                const IR = idleRestless.update(delta, isSpeaking, waifu);
                const rs = IR.rs;

                // ── Bones ──
                const hum   = currentVrm.humanoid;
                const chest = hum?.getNormalizedBoneNode('chest');
                const head  = hum?.getNormalizedBoneNode('head');
                const hips  = hum?.getNormalizedBoneNode('hips');
                const spine = hum?.getNormalizedBoneNode('spine');
                const neck  = hum?.getNormalizedBoneNode('neck');
                const RUA   = hum?.getNormalizedBoneNode('rightUpperArm');
                const RLA   = hum?.getNormalizedBoneNode('rightLowerArm');
                const LUA   = hum?.getNormalizedBoneNode('leftUpperArm');

                // ── 1. Respiración — diferenciada por personaje ──
                if (chest) {
                    if (isNino) {
                        // Nino: respiración más contenida y tensa al hablar
                        chest.rotation.x = Math.sin(time*1.7)*0.016 + Math.sin(time*3.2)*0.006
                                         + Math.sin(time*4.8)*0.028*S + Math.sin(time*2.4)*0.014*rs;
                        chest.rotation.y = Math.sin(time*1.1)*0.009 + Math.sin(time*2.4)*0.022*S;
                        chest.rotation.z = Math.sin(time*0.85+1.2)*0.007*rs;
                    } else {
                        // Miku: respiración más amplia y rítmica (idol con energía)
                        chest.rotation.x = Math.sin(time*2.0)*0.022 + Math.sin(time*3.8)*0.009
                                         + Math.sin(time*5.5)*0.018*S + Math.sin(time*2.8)*0.016*rs;
                        chest.rotation.y = Math.sin(time*1.4)*0.012 + Math.sin(time*3.0)*0.026*S;
                        chest.rotation.z = Math.sin(time*1.1+0.8)*0.010*rs + Math.sin(time*2.2)*0.008*S;
                    }
                }
                if (spine) {
                    spine.rotation.x = Math.sin(time*1.8)*0.008 + Math.sin(time*3.8)*0.013*S;
                    spine.rotation.z = Math.sin(time*0.7)*0.006*rs
                                     + (isMiku ? Math.sin(time*1.8)*0.006*S : 0);
                }

                // ── 2. Cabeza — Nino más orgullosa (mentón arriba), Miku más curiosa ──
                if (head) {
                    let hX, hY, hZ;
                    if (isNino) {
                        // Nino: ligera inclinación hacia atrás (altiva), movimiento más seco
                        hX = -0.04 + Math.sin(time*0.75)*0.010
                           + (Math.sin(time*6.5)*0.030 + Math.sin(time*3.0)*0.018)*S
                           + Math.sin(time*1.05)*0.032*rs + 0.04*L;
                        hY = Math.sin(time*0.55)*0.022 + Math.cos(time*0.22)*0.013
                           + Math.sin(time*2.2)*0.042*S + Math.sin(time*1.7+0.5)*0.072*rs;
                        hZ = Math.cos(time*0.42)*0.016 + Math.sin(time*3.4)*0.025*S
                           + Math.sin(time*0.48)*0.011*rs;
                    } else {
                        // Miku: inclina la cabeza con curiosidad, más tilting y expresiva
                        hX = 0.02 + Math.sin(time*0.85)*0.014
                           + (Math.sin(time*7.2)*0.038 + Math.sin(time*3.4)*0.022)*S
                           + Math.sin(time*1.15)*0.028*rs + 0.06*L;
                        hY = Math.sin(time*0.65)*0.030 + Math.cos(time*0.28)*0.018
                           + Math.sin(time*2.6)*0.055*S + Math.sin(time*2.0+0.6)*0.090*rs;
                        hZ = Math.cos(time*0.50)*0.022 + Math.sin(time*4.0)*0.035*S
                           + Math.sin(time*0.55)*0.014*rs;
                    }

                    if (IR.action === 'lookAround') {
                        hY += Math.sin(IR.ap*2.2)*0.30;
                        hX += Math.sin(IR.ap*1.6)*0.08;
                    }
                    if (IR.action === 'fidget') hZ += Math.sin(IR.ap*4.2)*0.045;
                    if (IR.action === 'sway' && isMiku) {
                        hY += Math.sin(IR.ap*1.8)*0.12;
                        hZ += Math.sin(IR.ap*2.4)*0.06;
                    }
                    if (IR.action === 'armsCross' && isNino) {
                        // Cuando Nino se cruza de brazos, tuerce un poco la cabeza con orgullo
                        hY += Math.sin(Math.min(1, IR.ap/2.8)*Math.PI) * 0.10;
                        hZ += Math.sin(Math.min(1, IR.ap/2.8)*Math.PI) * 0.08;
                    }

                    // Blending más suave para Miku, más directo para Nino
                    const headSpeed = isNino ? 5.5 : 7.0;
                    head.rotation.x += (hX - head.rotation.x) * Math.min(1, delta * headSpeed);
                    head.rotation.y += (hY - head.rotation.y) * Math.min(1, delta * headSpeed);
                    head.rotation.z += (hZ - head.rotation.z) * Math.min(1, delta * headSpeed);
                }

                // ── 3. Cuello ──
                if (neck) {
                    neck.rotation.x = Math.sin(time*0.92)*0.006 + 0.010*S;
                    neck.rotation.y = Math.sin(time*(isNino ? 0.52 : 0.68))*0.012*rs;
                }

                // ── 4. Caderas — Miku con balanceo rítmico ──
                if (hips) {
                    if (isNino) {
                        hips.rotation.y = Math.sin(time*1.55)*0.010 + Math.sin(time*2.1)*0.018*S
                                        + Math.sin(time*2.3+0.8)*0.024*rs;
                        hips.rotation.z = Math.sin(time*1.25+1.0)*0.007*rs;
                    } else {
                        // Miku: cadera con ritmo musical suave, incluso en idle
                        hips.rotation.y = Math.sin(time*1.8)*0.014 + Math.sin(time*2.4)*0.022*S
                                        + Math.sin(time*2.6+0.9)*0.028*rs
                                        + Math.sin(time*1.2)*0.009;   // swaying base
                        hips.rotation.z = Math.sin(time*1.4+0.8)*0.010*rs
                                        + Math.sin(time*0.9)*0.007;
                    }
                    if (IR.action === 'shrug' || IR.action === 'sigh') {
                        hips.rotation.z += Math.sin(IR.ap*2.8)*0.038;
                    }
                    if (IR.action === 'sway' && isMiku) {
                        hips.rotation.z += Math.sin(IR.ap*2.0)*0.055;
                        hips.rotation.y += Math.sin(IR.ap*1.8)*0.04;
                    }
                }

                // ── 5. Gestos con brazos (solo fuera de la entrada) ──
                if (!isEntering) gestureSystem.update(currentVrm, delta, time, S, waifu);

                // ── Acción idle: arreglarse el pelo ──
                if (IR.action === 'hairTouch' && RUA) {
                    const c = Math.sin(Math.min(1, Math.max(0, IR.ap / 2.8)) * Math.PI);
                    const sign = currentVrm._isVRM1 ? -1 : 1;
                    // Nino: la mano va al lado del cuello/oreja (pose orgullosa)
                    // Miku: la mano sube más arriba, pose más coqueta
                    const targetZ = isNino ? -sign * 0.40 : -sign * 0.36;
                    const targetX = isNino ? -0.52 : -0.60;
                    RUA.rotation.z += (targetZ - RUA.rotation.z) * (c * Math.min(1, delta * 3.8));
                    RUA.rotation.x += (targetX - RUA.rotation.x) * (c * Math.min(1, delta * 3.8));
                    if (RLA) RLA.rotation.x += (-0.48 - RLA.rotation.x) * (c * Math.min(1, delta * 3.8));
                }

                // ── Acción tsundere: brazos cruzados (solo Nino) ──
                if (IR.action === 'armsCross' && isNino && LUA && RUA) {
                    const c = Math.sin(Math.min(1, Math.max(0, IR.ap / 2.8)) * Math.PI);
                    const sign = currentVrm._isVRM1 ? -1 : 1;
                    LUA.rotation.z += (sign * 0.72 - LUA.rotation.z) * (c * Math.min(1, delta * 3.2));
                    LUA.rotation.x += (0.55 - LUA.rotation.x) * (c * Math.min(1, delta * 3.2));
                    RUA.rotation.z += (-sign * 0.72 - RUA.rotation.z) * (c * Math.min(1, delta * 3.2));
                    RUA.rotation.x += (-0.55 - RUA.rotation.x) * (c * Math.min(1, delta * 3.2));
                }

                // ── Acción Miku: sway rítmico de brazos ──
                if (IR.action === 'sway' && isMiku && LUA && RUA) {
                    const c = Math.sin(Math.min(1, IR.ap / 2.8) * Math.PI);
                    const sign = currentVrm._isVRM1 ? -1 : 1;
                    LUA.rotation.z += (sign * 1.15 - LUA.rotation.z) * (c * Math.min(1, delta * 2.5));
                    RUA.rotation.z += (-sign * 1.15 - RUA.rotation.z) * (c * Math.min(1, delta * 2.5));
                    LUA.rotation.x += (Math.sin(IR.ap*2.0)*0.18 - LUA.rotation.x) * (c * Math.min(1, delta * 2.5));
                    RUA.rotation.x += (-Math.sin(IR.ap*2.0)*0.18 - RUA.rotation.x) * (c * Math.min(1, delta * 2.5));
                }

                // ── 6. Lip-sync — más variado y ajustado por personaje ──
                if (S > 0.02) {
                    const cycle = Math.sin(time*14)*0.42 + Math.sin(time*23)*0.28 + 0.30;
                    const mo    = Math.max(0, Math.min(1, cycle)) * S;
                    const vw    = (Math.sin(time*5.2) + 1) * 0.5;
                    const vowelX = (Math.sin(time*3.8) + 1) * 0.5;
                    expressionController.set('aa', mo*(0.80-vw*0.30),         14);
                    expressionController.set('oh', mo*vw*0.62,                 14);
                    expressionController.set('ih', mo*0.25*(1-vw),             14);
                    expressionController.set('ee', mo*0.18*vowelX,             14);
                    expressionController.set('ou', mo*0.12*(1-vowelX),         14);

                    if (isNino) {
                        // Nino: expresión happy contenida + pequeño angry cuando habla fuerte
                        expressionController.set('happy',   0.22+S*0.12,   5);
                        expressionController.set('angry',   S*0.12,         4);
                        expressionController.set('relaxed', 0.08,           4);
                    } else {
                        // Miku: siempre alegre y radiante al hablar
                        expressionController.set('happy',   0.42+S*0.18,   5);
                        expressionController.set('relaxed', 0.15,           4);
                        expressionController.set('surprised', S*0.10,       4);
                    }
                } else {
                    expressionController.set('aa',  0, 10);
                    expressionController.set('oh',  0, 10);
                    expressionController.set('ih',  0, 10);
                    expressionController.set('ou',  0, 10);
                    expressionController.set('ee',  0, 10);
                    if (isNino) {
                        expressionController.set('happy',   0.14,  3);
                        expressionController.set('relaxed', 0.06,  3);
                        expressionController.set('angry',   0,     3);
                    } else {
                        expressionController.set('happy',   0.30,  3);
                        expressionController.set('relaxed', 0.12,  3);
                    }
                }

                // ── 7. Expresiones de estado por personaje ──
                if (rs > 0.1 && IR.phase === 2) {
                    if (isNino) {
                        // Nino aburrrida: cara de pocos amigos
                        expressionController.set('angry', 0.20*rs,             2);
                        expressionController.set('sad',   0.10*rs,             2);
                        expressionController.set('happy', Math.max(0, 0.14-rs*0.14), 3);
                    } else {
                        // Miku aburrida: algo melancólica pero sutil
                        expressionController.set('sad',   0.18*rs,             2);
                        expressionController.set('happy', Math.max(0, 0.30-rs*0.20), 3);
                    }
                } else {
                    expressionController.set('sad',   0, 3);
                    if (isNino) expressionController.set('angry', 0, 3);
                }

                if (IR.action === 'sigh') {
                    expressionController.set('relaxed', Math.sin(IR.ap*Math.PI/1.1)*0.50, 5);
                }
                expressionController.set('surprised', L*(isNino ? 0.22 : 0.35), isListening ? 8 : 5);

                // ── 8. Microexpresiones — distintas por personaje ──
                microTimer += delta;
                if (microTimer > nextMicro && !isSpeaking) {
                    microTimer = 0; nextMicro = 4 + Math.random() * 7;
                    const ninoMicros  = ['blush','angry','relaxed','surprised'];
                    const mikuMicros  = ['blush','happy','relaxed','surprised','wink'];
                    const pool = isNino ? ninoMicros : mikuMicros;
                    microExpr = pool[Math.floor(Math.random() * pool.length)];
                    microProg = 0;
                }
                if (microExpr !== 'none' && !isSpeaking) {
                    microProg += delta;
                    const intensity = isNino ? 0.30 : 0.38;
                    expressionController.set(microExpr, Math.max(0, Math.sin(microProg*Math.PI/1.3)*intensity), 7);
                    if (microProg > 1.3) microExpr = 'none';
                }

                // ── 9. Parpadeo orgánico — Nino parpadea más lento (pose seria) ──
                blinkTimer += delta;
                if (!isBlinking && blinkTimer > nextBlink) {
                    isBlinking = true; blinkTimer = 0;
                    // Nino: guiño doble menos frecuente. Miku: guiño coqueto más frecuente.
                    isDouble = Math.random() < (isNino ? 0.20 : 0.35); doubleStage = 0;
                }
                if (isBlinking) {
                    const blinkSpeed = isNino ? 0.13 : 0.11; // Miku parpadea más rápido
                    expressionController.set('blink', Math.max(0, Math.sin((blinkTimer/blinkSpeed)*Math.PI)), 32);
                    if (blinkTimer >= blinkSpeed) {
                        if (isDouble && doubleStage === 0) { doubleStage = 1; blinkTimer = -0.07; }
                        else {
                            expressionController.set('blink', 0, 32);
                            isBlinking = false; blinkTimer = 0;
                            const baseInterval = isNino ? 3.0 : 2.4; // Miku parpadea más seguido
                            nextBlink = (baseInterval + Math.random() * 3.0) * (rs > 0.3 ? 0.55 : 1.0);
                        }
                    }
                }

                // ── 10. Física de pelo ──
                hairPhysics.update(currentVrm, delta, head);

                // ── 11. Física de ropa ──
                clothPhysics.update(currentVrm, delta, hips, time);

                // ── 12. Expression flush ──
                expressionController.update(currentVrm, delta);

                currentVrm.update(delta);

            } catch(err) {
                if (!animate._warned) {
                    animate._warned = true;
                    console.warn('[Avatar] Error en el bucle de animación (solo se avisa una vez):', err);
                }
            }

            renderer.render(scene, camera);
        }

        cargarWaifu('nino');
        animate();
    </script>"""

with open('avatar.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Find offsets dynamically
idx_gesture = content.find('SPEECH GESTURE SYSTEM')
p = idx_gesture
for _ in range(3):
    p = content.rfind(chr(10), 0, p)
GESTURE_START = p + 1

ANIMATE_END = content.find('animate();', 62000)
ANIMATE_END = content.find('\n', ANIMATE_END) + 1

assert 'SPEECH GESTURE SYSTEM' in content[GESTURE_START:GESTURE_START+200], "Gesture marker not found"
print("Gesture start:", GESTURE_START)
print("Animate end:", ANIMATE_END)

new_content = content[:GESTURE_START] + NEW_BLOCK + content[ANIMATE_END:]

with open('avatar.html', 'w', encoding='utf-8') as f:
    f.write(new_content)

print("Done! New file size:", len(new_content))
