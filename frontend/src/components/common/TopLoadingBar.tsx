// ==========================================
// TopLoadingBar — global page-top progress bar
// Shows whenever anything is loading (network
// requests via loadingBus, or route changes).
// The fill width reflects how much has loaded.
// Developed by NTS Novalytix
// ==========================================

import React, { useEffect, useRef, useState } from 'react';
import { loadingBus } from '../../services/loadingBus';

const BAR_COLOR = '#0071e2';

export const TopLoadingBar: React.FC = () => {
  const [progress, setProgress] = useState(0);
  const [visible, setVisible] = useState(false);

  const progressRef = useRef(0);
  const visibleRef = useRef(false);
  const trickleRef = useRef<number | null>(null);
  const hideRef = useRef<number | null>(null);
  const watchdogRef = useRef<number | null>(null);

  const setP = (v: number) => {
    progressRef.current = v;
    setProgress(v);
  };

  const clearTrickle = () => {
    if (trickleRef.current) {
      clearInterval(trickleRef.current);
      trickleRef.current = null;
    }
  };

  const clearWatchdog = () => {
    if (watchdogRef.current) {
      clearTimeout(watchdogRef.current);
      watchdogRef.current = null;
    }
  };

  const begin = () => {
    if (hideRef.current) {
      clearTimeout(hideRef.current);
      hideRef.current = null;
    }
    if (!visibleRef.current) {
      visibleRef.current = true;
      setVisible(true);
      setP(8);
    }
    clearTrickle();
    // Trickle towards ~90% while work is pending, slowing as it approaches.
    trickleRef.current = window.setInterval(() => {
      const cur = progressRef.current;
      if (cur < 90) setP(Math.min(90, cur + Math.max(0.6, (90 - cur) * 0.08)));
    }, 180);

    // Safety watchdog: never let the bar hang forever if a background request
    // stalls or never settles (e.g. offline backend). Always drive it home.
    clearWatchdog();
    watchdogRef.current = window.setTimeout(() => finish(), 6000);
  };

  const finish = () => {
    if (!visibleRef.current) return;
    clearTrickle();
    clearWatchdog();
    // Snap the fill all the way to 100% so it is unmistakable the page is ready,
    // then hold briefly at full width before fading out.
    setP(100);
    hideRef.current = window.setTimeout(() => {
      visibleRef.current = false;
      setVisible(false);
      setP(0);
    }, 450);
  };

  // React to all in-flight network activity.
  useEffect(() => {
    const unsub = loadingBus.subscribe(active => {
      if (active > 0) begin();
      else if (visibleRef.current) finish();
    });
    return () => {
      unsub();
      clearTrickle();
      clearWatchdog();
      if (hideRef.current) clearTimeout(hideRef.current);
    };
  }, []);

  // Also pulse on every route change so navigation always feels responsive,
  // even when a page renders without a network call. Detected via the History
  // API so this works at the app root (outside the router), covering the login
  // page too.
  useEffect(() => {
    let t: number | null = null;
    const pulse = () => {
      begin();
      if (t) clearTimeout(t);
      // A route change means the new page has mounted and rendered from the
      // local store. Wait for the paint to settle, then always complete the bar
      // to 100% so the user clearly sees loading has finished — even if some
      // background network request (e.g. backend sync) is still in flight.
      t = window.setTimeout(() => {
        requestAnimationFrame(() => requestAnimationFrame(() => finish()));
      }, 350);
    };

    const origPush = history.pushState;
    const origReplace = history.replaceState;
    history.pushState = function (...args) {
      const r = origPush.apply(this, args as any);
      pulse();
      return r;
    };
    history.replaceState = function (...args) {
      const r = origReplace.apply(this, args as any);
      pulse();
      return r;
    };
    window.addEventListener('popstate', pulse);

    return () => {
      history.pushState = origPush;
      history.replaceState = origReplace;
      window.removeEventListener('popstate', pulse);
      if (t) clearTimeout(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div
      aria-hidden={!visible}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(progress)}
      style={{
        position: 'fixed',
        top: 0,
        left: 0,
        right: 0,
        height: '3px',
        zIndex: 99999,
        pointerEvents: 'none',
        opacity: visible ? 1 : 0,
        transition: 'opacity 250ms ease',
      }}
    >
      <div
        style={{
          position: 'relative',
          height: '100%',
          width: `${progress}%`,
          background: BAR_COLOR,
          borderRadius: '0 2px 2px 0',
          boxShadow: `0 0 8px ${BAR_COLOR}, 0 0 4px ${BAR_COLOR}`,
          transition: 'width 220ms ease',
        }}
      >
        {/* bright leading edge */}
        <div
          style={{
            position: 'absolute',
            right: 0,
            top: 0,
            height: '100%',
            width: '80px',
            transform: 'translateX(100%)',
            opacity: visible ? 1 : 0,
            boxShadow: `0 0 12px ${BAR_COLOR}, 0 0 6px ${BAR_COLOR}`,
          }}
        />
      </div>
    </div>
  );
};

export default TopLoadingBar;
