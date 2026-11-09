package com.introvoice.mic;

import android.content.Context;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.util.TypedValue;
import android.view.View;
import android.widget.TextView;

/** 统一的颜色 / 圆角 / 文字样式，保证界面观感与电脑端一致。 */
final class Ui {
    static final int BG = 0xFF14100E;
    static final int CARD = 0xFF1E1916;
    static final int CARD_SOFT = 0xFF231D19;
    static final int STROKE = 0xFF3A322C;
    static final int TEXT = 0xFFF5EFE6;
    static final int SUB = 0xFFB7ADA3;
    static final int ACCENT = 0xFFE8B24A;
    static final int ACCENT_DIM = 0xFF6B5324;
    static final int OK = 0xFF7BD88F;
    static final int WARN = 0xFFF0A35E;
    static final int ERR = 0xFFF0705A;

    private Ui() {
    }

    static int dp(Context c, float value) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value,
                c.getResources().getDisplayMetrics()));
    }

    static GradientDrawable round(int fill, float radiusDp, int strokeColor, float strokeDp) {
        GradientDrawable d = new GradientDrawable();
        d.setShape(GradientDrawable.RECTANGLE);
        d.setColor(fill);
        d.setCornerRadius(radiusDp);
        if (strokeDp > 0f) {
            d.setStroke(Math.max(1, Math.round(strokeDp)), strokeColor);
        }
        return d;
    }

    static TextView text(Context c, CharSequence s, float sp, int color, boolean bold) {
        TextView t = new TextView(c);
        t.setText(s);
        t.setTextSize(sp);
        t.setTextColor(color);
        if (bold) {
            t.setTypeface(t.getTypeface(), android.graphics.Typeface.BOLD);
        }
        return t;
    }

    static TextView section(Context c, String s) {
        TextView t = text(c, s, 11f, ACCENT, true);
        t.setLetterSpacing(0.14f);
        t.setAllCaps(false);
        return t;
    }

    static void setMargins(View v, int l, int t, int r, int b) {
        if (v.getLayoutParams() instanceof android.view.ViewGroup.MarginLayoutParams) {
            android.view.ViewGroup.MarginLayoutParams lp =
                    (android.view.ViewGroup.MarginLayoutParams) v.getLayoutParams();
            lp.setMargins(l, t, r, b);
            v.setLayoutParams(lp);
        }
    }

    static boolean atLeast(int api) {
        return Build.VERSION.SDK_INT >= api;
    }

    static int alpha(int color, float factor) {
        return Color.argb(Math.round(255 * factor), Color.red(color), Color.green(color), Color.blue(color));
    }
}
