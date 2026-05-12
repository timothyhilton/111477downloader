#!/usr/bin/env zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
IN_DIR="$SCRIPT_DIR/out"
OUT_DIR="$SCRIPT_DIR/out-transcoded"
MAX_JOBS=3

if (( MAX_JOBS < 1 )); then
  echo "MAX_JOBS must be at least 1" >&2
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg is required. Install it with: brew install ffmpeg" >&2
  exit 1
fi

if ! command -v ffprobe >/dev/null 2>&1; then
  echo "ffprobe is required. It is included with ffmpeg." >&2
  exit 1
fi

if [[ ! -d "$IN_DIR" ]]; then
  echo "Input folder not found: $IN_DIR" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
setopt null_glob
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

videos=(
  "$IN_DIR"/*.(mp4|mkv|avi|mov|m4v|webm|flv|wmv|mpg|mpeg|ts)(N)
)

if (( ${#videos[@]} == 0 )); then
  echo "No video files found in $IN_DIR"
  exit 0
fi

format_time() {
  local seconds=$1
  (( seconds < 0 )) && seconds=0
  printf "%02d:%02d:%02d" $(( seconds / 3600 )) $(( seconds % 3600 / 60 )) $(( seconds % 60 ))
}

terminal_width() {
  local width="${COLUMNS:-0}"

  if ! [[ "$width" == <-> ]] || (( width < 20 )); then
    width="$(tput cols 2>/dev/null || print 80)"
  fi

  if ! [[ "$width" == <-> ]] || (( width < 20 )); then
    width=80
  fi

  print "$width"
}

fit_to_terminal_line() {
  local line=$1
  local width="$(terminal_width)"
  local max=$(( width > 1 ? width - 1 : 1 ))
  local keep

  if (( ${#line} <= max )); then
    print -rn -- "$line"
    return
  fi

  if (( max > 3 )); then
    keep=$(( max - 3 ))
    print -rn -- "${line[1,$keep]}..."
  else
    print -rn -- "${line[1,$max]}"
  fi
}

episode_label() {
  local name=$1

  if [[ "$name" =~ '[sS][0-9][0-9][eE][0-9][0-9]' ]]; then
    print -rn -- "${MATCH:u}"
  else
    print -rn -- "unknown"
  fi
}

duration_seconds() {
  ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$1" \
    | awk '{ printf "%d", $1 }'
}

job_percent() {
  local id=$1
  local status_file="$TMP_DIR/$id.status"
  local progress_file="$TMP_DIR/$id.progress"
  local state input_name output_name duration out_us percent

  if [[ ! -f "$status_file" ]]; then
    print 0
    return
  fi

  IFS='|' read -r state input_name output_name duration < "$status_file"
  case "$state" in
    done) print 100; return ;;
    failed) print 0; return ;;
  esac

  out_us="$(awk -F= '/^out_time_(us|ms)=/ { v=$2 } END { print v+0 }' "$progress_file" 2>/dev/null || true)"
  if (( duration > 0 && out_us > 0 )); then
    percent=$(( out_us / 1000000 * 100 / duration ))
    (( percent > 100 )) && percent=100
    print "$percent"
  else
    print 0
  fi
}

job_running() {
  local pid=$1
  kill -0 "$pid" 2>/dev/null
}

start_job() {
  local id=$1 input=$2 output=$3
  local progress="$TMP_DIR/$id.progress"
  local status_file="$TMP_DIR/$id.status"
  local error_log="$TMP_DIR/$id.err"
  local temp_output="$output.part-$id.mp4"
  local duration="$(duration_seconds "$input")"

  print -r -- "running|${input:t}|${output:t}|$duration" > "$status_file"

  (
    rm -f "$progress" "$error_log" "$temp_output"

    if ffmpeg -hide_banner -nostdin -nostats -loglevel error -y \
      -i "$input" \
      -map 0:v:0 \
      -map '0:a:0?' \
      -sn \
      -vf "scale=-2:720" \
      -c:v h264_videotoolbox -b:v 3500k -maxrate 3000k -bufsize 6000k -pix_fmt yuv420p \
      -c:a aac -b:a 128k -ac 2 \
      -movflags +faststart \
      -progress "$progress" \
      "$temp_output" 2> "$error_log"; then
      mv "$temp_output" "$output"
      print -r -- "done|${input:t}|${output:t}|$duration" > "$status_file"
    else
      rm -f "$temp_output"
      print -r -- "failed|${input:t}|${output:t}|$duration" > "$status_file"
    fi
  ) &

  pids+=($!)
  ids+=($id)
}

active_job_summary() {
  local id=$1
  local status_file="$TMP_DIR/$id.status"
  local progress_file="$TMP_DIR/$id.progress"
  local state input_name output_name duration out_us speed percent eta done_seconds label

  if [[ ! -f "$status_file" ]]; then
    print -rn -- "#$id starting"
    return
  fi

  IFS='|' read -r state input_name output_name duration < "$status_file"
  out_us="$(awk -F= '/^out_time_(us|ms)=/ { v=$2 } END { print v+0 }' "$progress_file" 2>/dev/null || true)"
  speed="$(awk -F= '/^speed=/ { v=$2 } END { print v }' "$progress_file" 2>/dev/null || true)"

  done_seconds=$(( out_us / 1000000 ))
  if (( duration > 0 && done_seconds > 0 )); then
    percent=$(( done_seconds * 100 / duration ))
    (( percent > 100 )) && percent=100
    eta="$(format_time $(( duration - done_seconds )))"
  else
    percent=0
    eta="--:--:--"
  fi

  label="$(episode_label "$input_name")"
  print -rn -- "$(printf "%s %3d%% ETA %s %s" "$label" "$percent" "$eta" "${speed:-?}")"
}

draw_status() {
  local done_count=0
  local failed_count=0
  local active_count=${#active_ids[@]}
  local finished_count queued_count line id status_file state input_name output_name duration printed_lines extra i

  if (( rendered_lines > 0 )); then
    printf "\033[%dA" "$rendered_lines"
  fi

  for id in "${ids[@]}"; do
    status_file="$TMP_DIR/$id.status"
    [[ -f "$status_file" ]] || continue
    IFS='|' read -r state input_name output_name duration < "$status_file"

    case "$state" in
      done) (( ++done_count )) ;;
      failed) (( ++failed_count )) ;;
    esac
  done

  finished_count=$(( done_count + failed_count + skipped_count ))
  queued_count=$(( ${#videos[@]} - finished_count - active_count ))
  (( queued_count < 0 )) && queued_count=0

  line="$(printf "%d/%d finished" "$finished_count" "${#videos[@]}")"
  (( skipped_count > 0 )) && line+=" (${skipped_count} skipped)"
  (( failed_count > 0 )) && line+=" (${failed_count} failed)"
  line+=" | ${active_count} active | ${queued_count} queued"

  printf "\033[2K"
  fit_to_terminal_line "$line"
  print
  printed_lines=1

  for id in "${active_ids[@]}"; do
    printf "\033[2K"
    fit_to_terminal_line "$(active_job_summary "$id")"
    print
    (( ++printed_lines ))
  done

  extra=$(( rendered_lines - printed_lines ))
  if (( extra > 0 )); then
    for (( i = 1; i <= extra; i++ )); do
      printf "\033[2K\n"
    done
    printf "\033[%dA" "$extra"
  fi

  rendered_lines=$printed_lines
}

remove_finished_jobs() {
  local next_active_ids=()
  local next_active_pids=()
  local id pid i

  for (( i = 1; i <= ${#active_ids[@]}; i++ )); do
    id=${active_ids[$i]}
    pid=${active_pids[$i]}

    if job_running "$pid"; then
      next_active_ids+=($id)
      next_active_pids+=($pid)
    else
      wait "$pid" || true
    fi
  done

  active_ids=("${next_active_ids[@]}")
  active_pids=("${next_active_pids[@]}")
}

can_start_next_job() {
  local active_count=${#active_ids[@]}
  local stagger_percent=$(( 100 / MAX_JOBS ))
  local id percent target i

  (( active_count >= MAX_JOBS )) && return 1
  (( active_count == 0 )) && return 0

  for (( i = 1; i <= active_count; i++ )); do
    id=${active_ids[$i]}
    percent=$(job_percent "$id")
    target=$(( stagger_percent * (active_count - i + 1) ))

    if (( percent < target )); then
      return 1
    fi
  done

  return 0
}

start_scheduled_job() {
  local input=$1
  local output="$OUT_DIR/${input:t:r}.mp4"

  if [[ -f "$output" ]]; then
    (( ++skipped_count ))
    return 0
  fi

  (( ++job_id ))
  start_job "$job_id" "$input" "$output"
  active_ids+=($job_id)
  active_pids+=(${pids[-1]})
  return 0
}

run_scheduled_transcodes() {
  local next_video=1

  while true; do
    remove_finished_jobs

    while (( next_video <= ${#videos[@]} )) && can_start_next_job; do
      start_scheduled_job "${videos[$next_video]}"
      (( next_video++ ))
      remove_finished_jobs
    done

    draw_status

    (( next_video > ${#videos[@]} && ${#active_ids[@]} == 0 )) && break
    sleep 1
  done

  for pid in "${pids[@]}"; do
    wait "$pid" || true
  done

  draw_status
  print
  active_pids=()
  active_ids=()
}

pids=()
ids=()
active_pids=()
active_ids=()
job_id=0
skipped_count=0
rendered_lines=0

run_scheduled_transcodes

echo "Done. Transcoded files are in $OUT_DIR"
