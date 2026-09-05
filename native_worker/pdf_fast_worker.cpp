#define NOMINMAX

#include <mupdf/classes.h>
#include <mupdf/classes2.h>

#include <windows.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cctype>
#include <cmath>
#include <condition_variable>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <mutex>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_set>
#include <vector>

namespace fs = std::filesystem;

namespace {

constexpr int kProtocolVersion = 3;
constexpr int kMaximumThreads = 12;
constexpr size_t kMaximumTasks = 4096;
constexpr size_t kMaximumMergeSources = 100;
constexpr uintmax_t kMaximumManifestBytes = 8ULL * 1024ULL * 1024ULL;
constexpr size_t kMaximumManifestLineBytes = 1024;
constexpr size_t kMaximumMergeLineBytes = 32ULL * 1024ULL;
constexpr double kMaximumCoordinate = 1'000'000.0;
constexpr double kMaximumPixelsPerTask = 100'000'000.0;
constexpr double kMaximumPixelsPerBatch = 2'000'000'000.0;

struct RenderTask {
    int id = 0;
    int page = 0;
    float x0 = 0;
    float y0 = 0;
    float x1 = 0;
    float y1 = 0;
    int dpi = 180;
    int quality = 85;
    std::string filename;
    int group = 0;
};

struct MergeSource {
    int id = 0;
    fs::path path;
};

std::string utf8(const std::wstring& value) {
    if (value.empty()) {
        return {};
    }
    const int size = WideCharToMultiByte(
        CP_UTF8, WC_ERR_INVALID_CHARS, value.data(),
        static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
    if (size <= 0) {
        throw std::runtime_error("Unable to convert a Windows path to UTF-8.");
    }
    std::string result(static_cast<size_t>(size), '\0');
    WideCharToMultiByte(
        CP_UTF8, WC_ERR_INVALID_CHARS, value.data(),
        static_cast<int>(value.size()), result.data(), size, nullptr, nullptr);
    return result;
}

std::string json_escape(const std::string& value) {
    std::ostringstream output;
    for (const unsigned char character : value) {
        switch (character) {
        case '\\': output << "\\\\"; break;
        case '"': output << "\\\""; break;
        case '\b': output << "\\b"; break;
        case '\f': output << "\\f"; break;
        case '\n': output << "\\n"; break;
        case '\r': output << "\\r"; break;
        case '\t': output << "\\t"; break;
        default:
            if (character < 0x20) {
                output << "\\u" << std::hex << std::setw(4)
                       << std::setfill('0') << static_cast<int>(character)
                       << std::dec;
            } else {
                output << character;
            }
        }
    }
    return output.str();
}

std::vector<std::string> split_tabs(const std::string& line) {
    std::vector<std::string> values;
    std::string value;
    std::istringstream input(line);
    while (std::getline(input, value, '\t')) {
        values.push_back(value);
    }
    return values;
}

int parse_bounded_integer(
    const std::string& value,
    const char* label,
    int minimum,
    int maximum) {
    size_t parsed_characters = 0;
    long long parsed = 0;
    try {
        parsed = std::stoll(value, &parsed_characters, 10);
    } catch (const std::exception&) {
        throw std::runtime_error(std::string(label) + " is not an integer.");
    }
    if (parsed_characters != value.size() || parsed < minimum ||
        parsed > maximum) {
        throw std::runtime_error(
            std::string(label) + " is outside the safety range.");
    }
    return static_cast<int>(parsed);
}

float parse_coordinate(const std::string& value) {
    size_t parsed_characters = 0;
    float parsed = 0;
    try {
        parsed = std::stof(value, &parsed_characters);
    } catch (const std::exception&) {
        throw std::runtime_error("Figure coordinate is not numeric.");
    }
    if (parsed_characters != value.size() || !std::isfinite(parsed) ||
        std::abs(static_cast<double>(parsed)) > kMaximumCoordinate) {
        throw std::runtime_error("Figure coordinate is outside the safety range.");
    }
    return parsed;
}

bool safe_jpeg_filename(const std::string& value) {
    if (value.empty() || value.size() > 128 ||
        value.size() < 4 || value.substr(value.size() - 4) != ".jpg") {
        return false;
    }
    return std::all_of(value.begin(), value.end(), [](unsigned char character) {
        return (character >= 'a' && character <= 'z') ||
               (character >= 'A' && character <= 'Z') ||
               (character >= '0' && character <= '9') ||
               character == '.' || character == '_' || character == '-';
    });
}

std::vector<RenderTask> read_manifest(const fs::path& path) {
    if (!fs::is_regular_file(path) ||
        fs::file_size(path) > kMaximumManifestBytes) {
        throw std::runtime_error(
            "Render manifest is not a regular file or exceeds 8 MiB.");
    }
    std::ifstream input(path, std::ios::binary);
    if (!input) {
        throw std::runtime_error("Unable to open render manifest.");
    }
    std::vector<RenderTask> tasks;
    std::unordered_set<int> task_ids;
    std::unordered_set<std::string> filenames;
    double total_pixels = 0.0;
    std::string line;
    int line_number = 0;
    while (std::getline(input, line)) {
        ++line_number;
        if (!line.empty() && line.back() == '\r') {
            line.pop_back();
        }
        if (line.empty() || line.size() > kMaximumManifestLineBytes) {
            throw std::runtime_error(
                "Invalid manifest row " + std::to_string(line_number) + ".");
        }
        if (tasks.size() >= kMaximumTasks) {
            throw std::runtime_error("Render manifest exceeds 4096 tasks.");
        }
        const auto fields = split_tabs(line);
        if (fields.size() != 10) {
            throw std::runtime_error(
                "Malformed manifest row " + std::to_string(line_number) + ".");
        }
        RenderTask task;
        task.id = parse_bounded_integer(fields[0], "Task id", 0, 1'000'000);
        task.page = parse_bounded_integer(
            fields[1], "Page number", 0, 1'000'000);
        task.x0 = parse_coordinate(fields[2]);
        task.y0 = parse_coordinate(fields[3]);
        task.x1 = parse_coordinate(fields[4]);
        task.y1 = parse_coordinate(fields[5]);
        task.dpi = parse_bounded_integer(fields[6], "DPI", 24, 1200);
        task.quality = parse_bounded_integer(
            fields[7], "JPEG quality", 35, 100);
        task.filename = fields[8];
        task.group = parse_bounded_integer(
            fields[9], "Render group", 0, 1'000'000);
        const double width =
            static_cast<double>(task.x1 - task.x0) * task.dpi / 72.0;
        const double height =
            static_cast<double>(task.y1 - task.y0) * task.dpi / 72.0;
        const double task_pixels = width * height;
        if (task.x1 <= task.x0 || task.y1 <= task.y0 ||
            !safe_jpeg_filename(task.filename) ||
            !task_ids.insert(task.id).second ||
            !filenames.insert(task.filename).second ||
            task_pixels > kMaximumPixelsPerTask) {
            throw std::runtime_error(
                "Invalid render task on manifest row " +
                std::to_string(line_number) + ".");
        }
        total_pixels += task_pixels;
        if (total_pixels > kMaximumPixelsPerBatch) {
            throw std::runtime_error(
                "Render manifest exceeds the 2-gigapixel safety limit.");
        }
        tasks.push_back(std::move(task));
    }
    if (tasks.empty()) {
        throw std::runtime_error("Render manifest is empty.");
    }
    return tasks;
}

std::vector<MergeSource> read_merge_manifest(const fs::path& path) {
    if (!fs::is_regular_file(path) ||
        fs::file_size(path) > kMaximumManifestBytes) {
        throw std::runtime_error(
            "Merge manifest is not a regular file or exceeds 8 MiB.");
    }
    std::ifstream input(path, std::ios::binary);
    if (!input) {
        throw std::runtime_error("Unable to open merge manifest.");
    }
    std::vector<MergeSource> sources;
    std::string line;
    while (std::getline(input, line)) {
        if (!line.empty() && line.back() == '\r') {
            line.pop_back();
        }
        if (line.empty() || line.size() > kMaximumMergeLineBytes ||
            sources.size() >= kMaximumMergeSources) {
            throw std::runtime_error("Invalid merge manifest row.");
        }
        const auto fields = split_tabs(line);
        if (fields.size() != 2) {
            throw std::runtime_error("Malformed merge manifest row.");
        }
        MergeSource source;
        source.id = parse_bounded_integer(
            fields[0], "Merge source id", 0,
            static_cast<int>(kMaximumMergeSources - 1));
        if (source.id != static_cast<int>(sources.size())) {
            throw std::runtime_error("Merge source ids must be consecutive.");
        }
        source.path = fs::u8path(fields[1]);
        if (!fs::is_regular_file(source.path)) {
            throw std::runtime_error("Merge input is not a regular file.");
        }
        std::string extension = source.path.extension().string();
        std::transform(
            extension.begin(), extension.end(), extension.begin(),
            [](unsigned char character) {
                return static_cast<char>(std::tolower(character));
            });
        if (extension != ".pdf") {
            throw std::runtime_error("Merge input must use the .pdf extension.");
        }
        sources.push_back(std::move(source));
    }
    if (sources.size() < 2) {
        throw std::runtime_error("Native merge requires at least two PDFs.");
    }
    return sources;
}

bool same_region(const RenderTask& left, const RenderTask& right) {
    constexpr float tolerance = 0.001F;
    return left.page == right.page &&
           std::abs(left.x0 - right.x0) <= tolerance &&
           std::abs(left.y0 - right.y0) <= tolerance &&
           std::abs(left.x1 - right.x1) <= tolerance &&
           std::abs(left.y1 - right.y1) <= tolerance;
}

mupdf::FzPixmap render_master(
    const mupdf::FzDisplayList& display_list,
    const RenderTask& task,
    int dpi) {
    const float scale = static_cast<float>(dpi) / 72.0F;
    mupdf::FzMatrix matrix = mupdf::FzMatrix::fz_scale(scale, scale);
    mupdf::FzRect clip(task.x0, task.y0, task.x1, task.y1);
    mupdf::FzRect transformed(clip, matrix);
    mupdf::FzIrect bounds(transformed);
    if (bounds.fz_is_empty_irect()) {
        throw std::runtime_error("The requested Figure region is empty.");
    }

    mupdf::FzColorspace rgb(mupdf::FzColorspace::Fixed_RGB);
    mupdf::FzSeparations separations;
    mupdf::FzPixmap pixmap(rgb, bounds, separations, 0);
    pixmap.fz_clear_pixmap_with_value(255);

    // Apply the page transform exactly once. Passing it both to the draw
    // device and fz_run_page_contents would scale coordinates twice and crop
    // a completely different part of the page.
    mupdf::FzMatrix identity;
    mupdf::FzDevice draw(identity, pixmap, bounds);
    std::vector<fz_rect> cull_rectangles{*transformed.internal()};
    mupdf::FzDevice without_text(draw, cull_rectangles);
    mupdf::FzCookie cookie;
    display_list.fz_run_display_list(
        without_text, matrix, transformed, cookie);
    without_text.fz_close_device();

    return pixmap;
}

mupdf::FzPixmap scale_master(
    const mupdf::FzPixmap& master,
    int master_dpi,
    int target_dpi) {
    if (target_dpi == master_dpi) {
        return master;
    }
    const float ratio = static_cast<float>(target_dpi) /
                        static_cast<float>(master_dpi);
    const int width = std::max(
        1, static_cast<int>(std::lround(master.fz_pixmap_width() * ratio)));
    const int height = std::max(
        1, static_cast<int>(std::lround(master.fz_pixmap_height() * ratio)));
    mupdf::FzIrect scaled_bounds(0, 0, width, height);
    mupdf::FzPixmap output = master.fz_scale_pixmap(
        0.0F, 0.0F, static_cast<float>(width),
        static_cast<float>(height), scaled_bounds);
    if (!output) {
        throw std::runtime_error("MuPDF could not scale a Figure master.");
    }
    return output;
}

void save_variant(
    const mupdf::FzPixmap& pixmap,
    const RenderTask& task,
    const fs::path& output_directory) {
    const fs::path output_path = output_directory / fs::u8path(task.filename);
    const std::string output_utf8 = utf8(output_path.wstring());
    pixmap.fz_save_pixmap_as_jpeg(output_utf8.c_str(), task.quality);
}

void render_group(
    const mupdf::FzDocument& document,
    std::map<int, mupdf::FzDisplayList>& display_lists,
    const std::vector<RenderTask>& tasks,
    const fs::path& output_directory) {
    if (tasks.empty()) {
        return;
    }
    const RenderTask& first = tasks.front();
    for (const auto& task : tasks) {
        if (!same_region(first, task)) {
            throw std::runtime_error(
                "A render ladder group contains different Figure regions.");
        }
    }
    auto display = display_lists.find(first.page);
    if (display == display_lists.end()) {
        mupdf::FzPage page = document.fz_load_page(first.page);
        display = display_lists.emplace(
            first.page,
            mupdf::FzDisplayList::fz_new_display_list_from_page_contents(page)
        ).first;
    }
    const auto master_task = std::max_element(
        tasks.begin(), tasks.end(),
        [](const RenderTask& left, const RenderTask& right) {
            return left.dpi < right.dpi;
        });
    const int master_dpi = master_task->dpi;
    mupdf::FzPixmap master = render_master(
        display->second, first, master_dpi);
    std::map<int, mupdf::FzPixmap> scaled_pixmaps;
    scaled_pixmaps.emplace(master_dpi, master);
    for (const auto& task : tasks) {
        auto scaled = scaled_pixmaps.find(task.dpi);
        if (scaled == scaled_pixmaps.end()) {
            scaled = scaled_pixmaps.emplace(
                task.dpi, scale_master(master, master_dpi, task.dpi)
            ).first;
        }
        save_variant(scaled->second, task, output_directory);
    }
}

class RenderPool {
public:
    RenderPool(const fs::path& input_path, int requested_threads)
        : input_utf8_(utf8(input_path.wstring())),
          thread_count_(std::max(1, std::min(kMaximumThreads, requested_threads))) {
        workers_.reserve(static_cast<size_t>(thread_count_));
        for (int worker_number = 0; worker_number < thread_count_; ++worker_number) {
            workers_.emplace_back(&RenderPool::worker_loop, this, worker_number);
        }
        std::cout << "{\"type\":\"hello\",\"protocol\":"
                  << kProtocolVersion << ",\"threads\":" << thread_count_
                  << "}" << std::endl;
    }

    ~RenderPool() {
        {
            std::lock_guard<std::mutex> lock(state_mutex_);
            stopping_ = true;
            ++generation_;
        }
        start_condition_.notify_all();
        for (auto& worker : workers_) {
            if (worker.joinable()) {
                worker.join();
            }
        }
    }

    int run_batch(
        const fs::path& manifest_path,
        const fs::path& output_directory,
        bool ladder_mode = false) {
        auto tasks = read_manifest(manifest_path);
        fs::create_directories(output_directory);
        if (!fs::is_directory(output_directory)) {
            throw std::runtime_error("Render output is not a directory.");
        }
        for (const auto& task : tasks) {
            if (fs::exists(output_directory / fs::u8path(task.filename))) {
                throw std::runtime_error("A render output file already exists.");
            }
        }
        std::vector<std::vector<RenderTask>> groups;
        if (ladder_mode) {
            std::map<int, std::vector<RenderTask>> grouped;
            for (auto& task : tasks) {
                grouped[task.group].push_back(std::move(task));
            }
            groups.reserve(grouped.size());
            for (auto& [group_id, variants] : grouped) {
                static_cast<void>(group_id);
                groups.push_back(std::move(variants));
            }
        } else {
            groups.reserve(tasks.size());
            for (auto& task : tasks) {
                groups.push_back({std::move(task)});
            }
        }
        const auto started = std::chrono::steady_clock::now();
        {
            std::lock_guard<std::mutex> lock(state_mutex_);
            groups_ = std::move(groups);
            total_variants_ = 0;
            for (const auto& group : groups_) {
                total_variants_ += group.size();
            }
            output_directory_ = output_directory;
            next_group_.store(0);
            completed_.store(0);
            failed_.store(false);
            first_error_.clear();
            workers_pending_ = thread_count_;
            ++generation_;
        }
        start_condition_.notify_all();
        {
            std::unique_lock<std::mutex> lock(state_mutex_);
            finish_condition_.wait(lock, [&] { return workers_pending_ == 0; });
        }

        const auto elapsed =
            std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now() - started);
        std::lock_guard<std::mutex> output_lock(output_mutex_);
        if (failed_.load()) {
            std::cout << "{\"type\":\"result\",\"ok\":false,\"completed\":"
                      << completed_.load() << ",\"elapsed_ms\":"
                      << elapsed.count() << ",\"message\":\""
                      << json_escape(first_error_) << "\"}" << std::endl;
            return 4;
        }
        std::cout << "{\"type\":\"result\",\"ok\":true,\"completed\":"
                  << completed_.load() << ",\"elapsed_ms\":"
                  << elapsed.count() << ",\"master_renders\":"
                  << groups_.size() << ",\"variants\":"
                  << total_variants_ << "}" << std::endl;
        return 0;
    }

private:
    void fail(const std::string& message) {
        failed_.store(true);
        std::lock_guard<std::mutex> lock(state_mutex_);
        if (first_error_.empty()) {
            first_error_ = message;
        }
    }

    void worker_loop(int worker_number) {
        std::unique_ptr<mupdf::FzDocument> document;
        std::map<int, mupdf::FzDisplayList> display_lists;
        std::string startup_error;
        try {
            document = std::make_unique<mupdf::FzDocument>(input_utf8_.c_str());
        } catch (const std::exception& error) {
            startup_error = error.what();
        }

        size_t observed_generation = 0;
        while (true) {
            {
                std::unique_lock<std::mutex> lock(state_mutex_);
                start_condition_.wait(lock, [&] {
                    return stopping_ || generation_ != observed_generation;
                });
                if (stopping_) {
                    return;
                }
                observed_generation = generation_;
            }

            if (!startup_error.empty()) {
                fail(startup_error);
            } else {
                while (!failed_.load(std::memory_order_relaxed)) {
                    const size_t index = next_group_.fetch_add(1);
                    if (index >= groups_.size()) {
                        break;
                    }
                    const std::vector<RenderTask> tasks = groups_[index];
                    try {
                        render_group(
                            *document, display_lists, tasks, output_directory_);
                        for (const auto& task : tasks) {
                            const auto output_path =
                                output_directory_ / fs::u8path(task.filename);
                            const auto bytes = fs::file_size(output_path);
                            const int done = completed_.fetch_add(1) + 1;
                            std::lock_guard<std::mutex> lock(output_mutex_);
                            std::cout << "{\"type\":\"progress\",\"id\":"
                                      << task.id << ",\"completed\":" << done
                                      << ",\"total\":" << total_variants_
                                      << ",\"bytes\":" << bytes
                                      << ",\"worker\":" << worker_number
                                      << "}" << std::endl;
                        }
                    } catch (const std::exception& error) {
                        fail(error.what());
                        std::lock_guard<std::mutex> lock(output_mutex_);
                        std::cout << "{\"type\":\"task_error\",\"id\":"
                                  << tasks.front().id << ",\"message\":\""
                                  << json_escape(error.what()) << "\"}"
                                  << std::endl;
                    }
                }
            }

            {
                std::lock_guard<std::mutex> lock(state_mutex_);
                --workers_pending_;
                if (workers_pending_ == 0) {
                    finish_condition_.notify_one();
                }
            }
        }
    }

    std::string input_utf8_;
    int thread_count_;
    std::vector<std::thread> workers_;
    std::mutex state_mutex_;
    std::mutex output_mutex_;
    std::condition_variable start_condition_;
    std::condition_variable finish_condition_;
    std::vector<std::vector<RenderTask>> groups_;
    size_t total_variants_ = 0;
    fs::path output_directory_;
    std::atomic<size_t> next_group_{0};
    std::atomic<int> completed_{0};
    std::atomic<bool> failed_{false};
    bool stopping_ = false;
    size_t generation_ = 0;
    int workers_pending_ = 0;
    std::string first_error_;
};

void copy_merge_page(
    const mupdf::PdfDocument& destination,
    const mupdf::PdfDocument& source,
    const mupdf::PdfGraftMap& graft,
    int page_number) {
    static constexpr const char* kPageKeys[] = {
        "Contents", "Resources", "MediaBox", "CropBox", "BleedBox",
        "TrimBox", "ArtBox", "Rotate", "UserUnit",
    };
    const mupdf::PdfObj source_page = source.pdf_lookup_page_obj(page_number);
    mupdf::PdfObj destination_page = destination.pdf_new_dict(12);
    destination_page.pdf_dict_put(
        mupdf::PdfObj("Type"), mupdf::PdfObj("Page"));
    for (const char* key_name : kPageKeys) {
        const mupdf::PdfObj key(key_name);
        const mupdf::PdfObj value = source_page.pdf_dict_get_inheritable(key);
        if (value.m_internal != nullptr) {
            destination_page.pdf_dict_put(
                key, graft.pdf_graft_mapped_object(value));
        }
    }

    // Mirror PyMuPDF's page merge behavior: retain ordinary annotations,
    // while links are rebuilt after all pages receive their final offsets.
    const mupdf::PdfObj old_annotations =
        source_page.pdf_dict_get(mupdf::PdfObj("Annots"));
    const int annotation_count = old_annotations.pdf_array_len();
    if (annotation_count > 0) {
        mupdf::PdfObj new_annotations = destination_page.pdf_dict_put_array(
            mupdf::PdfObj("Annots"), annotation_count);
        for (int index = 0; index < annotation_count; ++index) {
            mupdf::PdfObj annotation = old_annotations.pdf_array_get(index);
            if (annotation.m_internal == nullptr || !annotation.pdf_is_dict() ||
                annotation.pdf_dict_gets("IRT").m_internal != nullptr) {
                continue;
            }
            const mupdf::PdfObj subtype = annotation.pdf_dict_gets("Subtype");
            if (subtype.pdf_name_eq(mupdf::PdfObj("Link")) ||
                subtype.pdf_name_eq(mupdf::PdfObj("Popup")) ||
                subtype.pdf_name_eq(mupdf::PdfObj("Widget"))) {
                continue;
            }
            annotation.pdf_dict_del(mupdf::PdfObj("Popup"));
            annotation.pdf_dict_del(mupdf::PdfObj("P"));
            const mupdf::PdfObj copied =
                graft.pdf_graft_mapped_object(annotation);
            new_annotations.pdf_array_push(destination.pdf_new_indirect(
                copied.pdf_to_num(), 0));
        }
    }
    const mupdf::PdfObj page_reference =
        destination.pdf_add_object(destination_page);
    destination.pdf_insert_page(-1, page_reference);
}

bool has_copyable_annotations(
    const mupdf::PdfDocument& source,
    int page_number) {
    const mupdf::PdfObj source_page = source.pdf_lookup_page_obj(page_number);
    const mupdf::PdfObj annotations =
        source_page.pdf_dict_get(mupdf::PdfObj("Annots"));
    const int annotation_count = annotations.pdf_array_len();
    for (int index = 0; index < annotation_count; ++index) {
        mupdf::PdfObj annotation = annotations.pdf_array_get(index);
        if (annotation.m_internal == nullptr || !annotation.pdf_is_dict() ||
            annotation.pdf_dict_gets("IRT").m_internal != nullptr) {
            continue;
        }
        const mupdf::PdfObj subtype = annotation.pdf_dict_gets("Subtype");
        if (!subtype.pdf_name_eq(mupdf::PdfObj("Link")) &&
            !subtype.pdf_name_eq(mupdf::PdfObj("Popup")) &&
            !subtype.pdf_name_eq(mupdf::PdfObj("Widget"))) {
            return true;
        }
    }
    return false;
}

int merge_documents(
    const fs::path& manifest_path,
    const fs::path& output_path) {
    const auto sources = read_merge_manifest(manifest_path);
    if (fs::exists(output_path)) {
        throw std::runtime_error("Merge output already exists.");
    }
    const auto started = std::chrono::steady_clock::now();
    mupdf::PdfDocument destination;
    int total_pages = 0;
    for (size_t index = 0; index < sources.size(); ++index) {
        const std::string source_utf8 = utf8(sources[index].path.wstring());
        mupdf::PdfDocument source(source_utf8.c_str());
        if (source.pdf_needs_password()) {
            throw std::runtime_error("Merge input is password protected.");
        }
        const int page_count = source.pdf_count_pages();
        if (page_count <= 0) {
            throw std::runtime_error("Merge input has no pages.");
        }
        mupdf::PdfGraftMap graft(destination);
        for (int page = 0; page < page_count; ++page) {
            if (has_copyable_annotations(source, page)) {
                copy_merge_page(destination, source, graft, page);
            } else {
                graft.pdf_graft_mapped_page(-1, source, page);
            }
        }
        total_pages += page_count;
        std::cout << "{\"type\":\"progress\",\"operation\":\"merge\","
                     "\"completed\":" << index + 1
                  << ",\"total\":" << sources.size()
                  << ",\"pages\":" << total_pages << "}" << std::endl;
    }
    mupdf::PdfWriteOptions options;
    options.do_garbage = 4;
    options.do_compress = 1;
    options.do_compress_images = 1;
    options.do_compress_fonts = 1;
    options.do_use_objstms = 1;
    options.do_encrypt = PDF_ENCRYPT_NONE;
    const std::string output_utf8 = utf8(output_path.wstring());
    destination.pdf_save_document(output_utf8.c_str(), options);
    const auto elapsed =
        std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now() - started);
    const uintmax_t output_bytes = fs::file_size(output_path);
    std::cout << "{\"type\":\"result\",\"ok\":true,"
                 "\"operation\":\"merge\",\"completed\":"
              << sources.size() << ",\"pages\":" << total_pages
              << ",\"bytes\":" << output_bytes
              << ",\"elapsed_ms\":" << elapsed.count() << "}"
              << std::endl;
    return 0;
}

int serve(const fs::path& input_path, int requested_threads) {
    RenderPool pool(input_path, requested_threads);
    std::string line;
    while (std::getline(std::cin, line)) {
        if (!line.empty() && line.back() == '\r') {
            line.pop_back();
        }
        if (line == "QUIT") {
            return 0;
        }
        const auto fields = split_tabs(line);
        if (fields.size() != 3 ||
            (fields[0] != "BATCH" && fields[0] != "LADDER")) {
            std::cout << "{\"type\":\"result\",\"ok\":false,"
                         "\"completed\":0,\"message\":\"Invalid server command.\"}"
                      << std::endl;
            continue;
        }
        pool.run_batch(
            fs::u8path(fields[1]), fs::u8path(fields[2]),
            fields[0] == "LADDER");
    }
    return 0;
}

std::wstring required_value(
    const std::vector<std::wstring>& arguments, const std::wstring& name) {
    for (size_t index = 0; index + 1 < arguments.size(); ++index) {
        if (arguments[index] == name) {
            return arguments[index + 1];
        }
    }
    throw std::runtime_error("Missing a required command-line option.");
}

int optional_integer(
    const std::vector<std::wstring>& arguments,
    const std::wstring& name,
    int fallback) {
    for (size_t index = 0; index + 1 < arguments.size(); ++index) {
        if (arguments[index] == name) {
            return std::stoi(arguments[index + 1]);
        }
    }
    return fallback;
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    SetConsoleOutputCP(CP_UTF8);
    try {
        std::vector<std::wstring> arguments(argv + 1, argv + argc);
        if (arguments.empty() ||
            (arguments[0] != L"render-batch" && arguments[0] != L"serve" &&
             arguments[0] != L"merge" && arguments[0] != L"--version")) {
            std::cerr << "Usage: pdf_fast_worker render-batch --input PDF "
                         "--manifest TSV --output-dir DIR [--threads N]\n"
                         "   or: pdf_fast_worker serve --input PDF [--threads N]\n"
                         "   or: pdf_fast_worker merge --manifest TSV --output PDF\n";
            return 2;
        }
        if (arguments[0] == L"--version") {
            std::cout << "{\"name\":\"pdf_fast_worker\",\"protocol\":"
                      << kProtocolVersion << "}" << std::endl;
            return 0;
        }

        mupdf::fz_register_document_handlers();
        if (arguments[0] == L"merge") {
            return merge_documents(
                fs::path(required_value(arguments, L"--manifest")),
                fs::path(required_value(arguments, L"--output")));
        }
        const fs::path input_path(required_value(arguments, L"--input"));
        const int threads = optional_integer(
            arguments,
            L"--threads",
            static_cast<int>(std::max(1U, std::thread::hardware_concurrency())));
        if (arguments[0] == L"serve") {
            return serve(input_path, threads);
        }
        const fs::path manifest_path(required_value(arguments, L"--manifest"));
        const fs::path output_directory(
            required_value(arguments, L"--output-dir"));
        RenderPool pool(input_path, threads);
        return pool.run_batch(manifest_path, output_directory);
    } catch (const std::exception& error) {
        std::cout << "{\"type\":\"result\",\"ok\":false,\"completed\":0,"
                     "\"message\":\""
                  << json_escape(error.what()) << "\"}" << std::endl;
        return 3;
    }
}
